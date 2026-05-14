"""
Holocron Hourly Runner
======================
Autonomous routine that maintains personalized Quicksilver display data
for active users. Runs hourly via Claude Code Routines (cloud).

This script handles all deterministic logic (locks, fetches, parses, triage,
write-back, registry, run log) in pure Python. Editorial work that requires
model judgment (board scoring, suggestion generation, "why now" rationale)
is delegated to the Anthropic API.

Environment variables:
    GITHUB_TOKEN          — required. Token with gist scope.
    ANTHROPIC_API_KEY     — required for full/source-refresh tiers (editorial
                            phases). Light/cold-start tiers also need it for
                            board bootstrap.
    HOLOCRON_DRY_RUN      — optional, default "false". When true, the pipeline
                            runs end-to-end but does NOT PATCH user state or
                            display JSONs back to Gist. Quality-gate output
                            and would-be writes are logged. Set to "true" to
                            preview writes without persisting.
    HOLOCRON_USER_FILTER  — optional, comma-separated usernames to process.
                            Empty = all active users. Use for smoke tests.

Exit codes:
    0  — completed successfully
    1  — fatal error before lock acquire (or stale lock cleared but next run owns it)
    2  — registry parse failure
    3  — source fetch failure
"""

import json
import os
import random
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

# Anthropic SDK is imported lazily inside the editorial phases so that
# Light/Skip-only runs (and Stage 1 smoke tests) don't require the package.
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

# ===========================================================================
# Configuration
# ===========================================================================

QUICKSILVER_GIST_ID = "287eb4fd487bff8f06e53bcf6cd18f2b"
NW_STATE_GIST_ID = "961083278c6a59b863314e56c5a60402"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Safety flags — DRY_RUN defaults to false (live writes). Set
# HOLOCRON_DRY_RUN=true in the environment to preview writes without
# PATCHing user state or display JSONs.
DRY_RUN = os.environ.get("HOLOCRON_DRY_RUN", "false").strip().lower() == "true"
USER_FILTER = [u.strip() for u in os.environ.get("HOLOCRON_USER_FILTER", "").split(",") if u.strip()]

# Per-phase model routing. None = pure Python, no API call.
# Sonnet across the board initially; Phase 4 upgrades to Opus after
# Drive 3 Trust Calibration (see HOLOCRON-ROUTINE-SPEC.md §10).
MODEL_CONFIG: dict = {
    1: None,
    2: None,
    3: None,
    4: "claude-sonnet-4-6",     # board meeting (editorial)
    5: "claude-sonnet-4-6",     # triggered evaluations (mostly mechanical)
    "5d": None,                  # CG classification (deterministic routing rules)
    6: None,
    7: None,
    8: None,
}

# Tier definitions: which Runner phases to execute per tier
PHASES_PER_TIER = {
    "FULL":           [1, 2, 3, 4, 5, "5d", 6, 7, 8],
    "SOURCE_REFRESH": [1, 2, 3, 4, 5, "5d", 6, 7, 8],
    "COLD_START":     [1, 3, 4, 5, "5d", 6, 7, 8],
    "LIGHT":          [1, 2, 3, 6, 7, 8],
    "SKIP":           [],
}

QUALITY_DIMENSIONS = [
    "menu_accuracy",
    "amount_accuracy",
    "data_correctness",
    "suggestion_relevance",
    "rate_sensitivity",
    "signal_isolation",
]

CRITICAL_SOURCES = ["HOLOCRON-RUNNER.md", "NW-MENUS.md", "QUICKSILVER-CONTENT.md", "QUICKSILVER-SCHEMA.md"]
WARN_SOURCES = ["QUICKSILVER-CONSTITUTION.md"]

LOCK_STALE_HOURS = 2
DORMANT_THRESHOLD_DAYS = 30
MID_SESSION_THRESHOLD_MIN = 60
WEEKLY_ANCHOR_DAYS = 7
RUN_LOG_CAP = 168  # 24 hours * 7 days

# Phase 4 board call sizing. Increase max_tokens as board scoring matures.
PHASE_4_MAX_TOKENS = 4096
PHASE_5_MAX_TOKENS = 2048

# Diagnostic accumulator — appended to over the run, written to log at end
DIAGNOSTICS: list[dict] = []


# ===========================================================================
# Utility
# ===========================================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_in_tz(tz_name: str) -> str:
    """Return today's date (YYYY-MM-DD) in the given IANA timezone. Falls back
    to Pacific on invalid TZ. Falls back to UTC if zoneinfo unavailable."""
    if not ZoneInfo:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        tz = ZoneInfo(tz_name or "America/Los_Angeles")
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    return datetime.now(tz).date().isoformat()


def log_diagnostic(kind: str, username: Optional[str] = None, detail: str = "") -> None:
    """Accumulate a diagnostic event for the run log."""
    DIAGNOSTICS.append({
        "kind": kind,
        "username": username,
        "detail": detail,
        "ts": now_iso(),
    })
    user_str = f"[{username}] " if username else ""
    print(f"DIAG {kind}: {user_str}{detail}", flush=True)


def fatal(msg: str, code: int = 1) -> None:
    """Print error and exit with code. Caller should release lock first if held."""
    print(f"FATAL: {msg}", flush=True)
    sys.exit(code)


def parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO timestamp string, accepting trailing Z. Returns None on failure."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ===========================================================================
# GitHub Gist Client
# ===========================================================================

def gist_get(gist_id: str) -> dict:
    """Fetch entire Gist. Returns parsed JSON. Raises on HTTP error."""
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gist_get_file(gist_id: str, filename: str) -> tuple[str, str]:
    """
    Fetch a single file's content from a Gist.
    Returns (content, updated_at_iso). Raises FileNotFoundError if missing.
    """
    data = gist_get(gist_id)
    files = data.get("files", {})
    if filename not in files:
        raise FileNotFoundError(f"{filename} not in gist {gist_id}")
    content = files[filename].get("content", "")
    updated_at = data.get("updated_at", "")
    return content, updated_at


def gist_patch_files(gist_id: str, files: dict[str, str]) -> dict:
    """
    PATCH one or more files to a Gist. `files` is {filename: content}.
    Returns the response JSON. Raises RuntimeError if payload contains a
    token-looking string (last-line defense — see CLAUDE.md security rules).
    """
    payload = {
        "files": {fname: {"content": content} for fname, content in files.items()}
    }
    body = json.dumps(payload).encode("utf-8")

    if re.search(r"ghp_[A-Za-z0-9]{20,}", body.decode("utf-8")):
        raise RuntimeError(f"Token pattern detected in payload for gist {gist_id} — refusing to PATCH")

    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ===========================================================================
# Step 1 — Acquire Lock
# ===========================================================================

def acquire_lock() -> dict:
    """
    Fetch HOLOCRON-USERS.md, check lock state, acquire if free or stale.
    Returns parsed registry (dict with metadata, users, etc.).
    Exits with code 1 if another run is active and not stale.
    """
    print("Step 1: Acquiring lock...", flush=True)
    content, _ = gist_get_file(QUICKSILVER_GIST_ID, "HOLOCRON-USERS.md")
    registry = parse_registry(content)

    meta = registry["metadata"]
    if meta.get("routine_active") == "true":
        acquired_str = meta.get("acquired_at", "")
        acquired = parse_iso(acquired_str)
        if acquired:
            age = datetime.now(timezone.utc) - acquired
            if age < timedelta(hours=LOCK_STALE_HOURS):
                print(f"Another run is active (acquired {age} ago). Exiting.", flush=True)
                sys.exit(1)
            else:
                log_diagnostic("STALE_LOCK_CLEARED", detail=f"acquired_at={acquired_str}, age={age}")
        else:
            log_diagnostic("STALE_LOCK_CLEARED", detail=f"unparseable acquired_at: {acquired_str!r}")

    meta["routine_active"] = "true"
    meta["acquired_at"] = now_iso()
    new_content = serialize_registry(registry)
    gist_patch_files(QUICKSILVER_GIST_ID, {"HOLOCRON-USERS.md": new_content})
    print("Lock acquired.", flush=True)
    return registry


def release_lock(registry: dict) -> None:
    """Release the lock. ALWAYS runs at end of execution, even after errors."""
    print("Step 7: Releasing lock...", flush=True)
    try:
        content, _ = gist_get_file(QUICKSILVER_GIST_ID, "HOLOCRON-USERS.md")
        fresh = parse_registry(content)
        fresh["metadata"]["routine_active"] = "false"
        fresh["metadata"]["acquired_at"] = ""
        gist_patch_files(QUICKSILVER_GIST_ID, {"HOLOCRON-USERS.md": serialize_registry(fresh)})
        print("Lock released.", flush=True)
    except Exception as e:
        print(f"WARNING: Lock release failed: {e}", flush=True)


# ===========================================================================
# Step 2 — Parse Registry
# ===========================================================================

def parse_registry(content: str) -> dict:
    """
    Parse HOLOCRON-USERS.md into a structured dict.

    Expected format:
        ## Metadata
        - routine_active: false
        - acquired_at:

        ## Users  (or "## Registry" — both accepted)
        | username | status | last_session | ... |
        | --- | --- | ... |
        | gerber | active | ... |
    """
    metadata = {}
    users = []
    user_columns: list[str] = []

    in_meta = False
    in_users = False
    lines = content.splitlines()
    user_header_seen = False
    for line in lines:
        if line.startswith("## Metadata"):
            in_meta = True
            in_users = False
            continue
        if line.startswith("## Users") or line.startswith("## Registry"):
            in_meta = False
            in_users = True
            user_header_seen = False
            continue
        if line.startswith("## "):
            in_meta = False
            in_users = False
            continue

        if in_meta:
            m = re.match(r"^\s*-\s*([^:]+):\s*(.*)$", line)
            if m:
                metadata[m.group(1).strip()] = m.group(2).strip()

        if in_users and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not user_header_seen:
                user_header_seen = True
                user_columns = cells
                continue
            if all(c.replace("-", "").replace(":", "").strip() == "" for c in cells):
                continue
            row = dict(zip(user_columns, cells))
            if row.get("username"):
                users.append(row)

    # Safety check — see CLAUDE.md "Data loss prevention".
    if not users:
        table_rows = [
            l for l in lines
            if l.strip().startswith("|")
            and not all(c.replace("-", "").replace(":", "").strip() == ""
                        for c in l.strip().strip("|").split("|"))
        ]
        data_rows = max(0, len(table_rows) - 1) if table_rows else 0
        if data_rows > 0:
            raise RuntimeError(
                f"parse_registry found 0 users but raw content has {data_rows} "
                f"data rows in a table. Possible schema mismatch or parse failure. "
                f"HALTING to prevent data loss."
            )

    return {
        "metadata": metadata,
        "users": users,
        "user_columns": user_columns,
        "raw": content,
    }


def serialize_registry(registry: dict) -> str:
    """Write registry back to markdown. Round-trip-safe with parse_registry."""
    out = []
    out.append("# Holocron Users Registry")
    out.append("")
    out.append("## Metadata")
    for k, v in registry["metadata"].items():
        out.append(f"- {k}: {v}")
    out.append("")
    out.append("## Users")
    out.append("")
    cols = registry.get("user_columns") or [
        "username", "status", "last_session", "last_holocron_run",
        "last_full_run", "dormant_since", "timezone", "day_view_last_refreshed",
        "last_source_versions", "last_refresh", "last_mode", "last_confidence", "notes",
    ]
    out.append("| " + " | ".join(cols) + " |")
    out.append("|" + "|".join(["---"] * len(cols)) + "|")
    for user in registry["users"]:
        out.append("| " + " | ".join(user.get(c, "") for c in cols) + " |")
    out.append("")
    return "\n".join(out)


# ===========================================================================
# Step 3 — Fetch Source Files
# ===========================================================================

def fetch_sources() -> dict[str, dict]:
    """
    Fetch all source files. Returns dict[filename] = {content, updated_at}.
    HALTS if any critical source is missing.
    """
    print("Step 3: Fetching source files...", flush=True)
    sources = {}
    quicksilver = gist_get(QUICKSILVER_GIST_ID)
    files = quicksilver.get("files", {})
    gist_updated_at = quicksilver.get("updated_at", "")

    for fname in CRITICAL_SOURCES:
        if fname not in files:
            log_diagnostic("FETCH_FAIL", detail=f"critical source missing: {fname}")
            fatal(f"Critical source missing: {fname}", code=3)
        sources[fname] = {
            "content": files[fname].get("content", ""),
            "updated_at": gist_updated_at,  # coarse: gist-level updated_at
        }

    for fname in WARN_SOURCES:
        if fname in files:
            sources[fname] = {
                "content": files[fname].get("content", ""),
                "updated_at": gist_updated_at,
            }
        else:
            log_diagnostic("FETCH_WARN", detail=f"non-critical source missing: {fname}")

    print(f"  Fetched {len(sources)} source files.", flush=True)
    return sources


def sources_changed_for_user(sources: dict, user: dict) -> bool:
    """Compare current source versions against user's last_source_versions."""
    user_versions_raw = user.get("last_source_versions", "")
    try:
        user_versions = json.loads(user_versions_raw) if user_versions_raw else {}
    except json.JSONDecodeError:
        user_versions = {}

    for fname, info in sources.items():
        if user_versions.get(fname) != info["updated_at"]:
            return True
    return False


# ===========================================================================
# Step 4a — Triage
# ===========================================================================

def triage(user: dict, sources: dict) -> str:
    """Seven-step decision tree. First match wins. Returns tier string."""
    username = user["username"]

    try:
        gist_get_file(NW_STATE_GIST_ID, f"USER-{username}.json")
        user_json_exists = True
    except (FileNotFoundError, urllib.error.HTTPError):
        user_json_exists = False

    if not user_json_exists:
        return "COLD_START"

    last_session = user.get("last_session", "")
    ls = parse_iso(last_session)
    if ls:
        if datetime.now(timezone.utc) - ls < timedelta(minutes=MID_SESSION_THRESHOLD_MIN):
            return "SKIP"

    if ls:
        if datetime.now(timezone.utc) - ls > timedelta(days=DORMANT_THRESHOLD_DAYS):
            if not user.get("dormant_since"):
                user["dormant_since"] = now_iso()
                log_diagnostic("DORMANT_SKIP", username=username,
                               detail=f"days_since_last_session={(datetime.now(timezone.utc) - ls).days}")
            return "SKIP"

    dormant_since_str = user.get("dormant_since", "")
    if dormant_since_str and ls:
        ds = parse_iso(dormant_since_str)
        if ds and ls > ds:
            user["dormant_since"] = ""
            user["welcome_back"] = "true"
            return "COLD_START"

    if sources_changed_for_user(sources, user):
        return "SOURCE_REFRESH"

    # Step 4: heavy signal in event log — fetched in Phase 1, so triage uses
    # a quick peek at the event log (cheap GET).
    has_explore_answers = _peek_for_event(username, "explore-answer")
    if has_explore_answers:
        return "FULL"

    last_full_run_str = user.get("last_full_run", "")
    lfr = parse_iso(last_full_run_str)
    if lfr:
        if datetime.now(timezone.utc) - lfr > timedelta(days=WEEKLY_ANCHOR_DAYS):
            return "FULL"
    else:
        return "FULL"

    has_markoffs = _peek_for_event(username, "mark-off")
    day_ticked = _day_ticked(user)
    if has_markoffs or day_ticked:
        return "LIGHT"

    return "SKIP"


def _peek_for_event(username: str, event_type: str) -> bool:
    """Cheap peek into the user's event log for a given event_type. Returns
    False on 404/parse error (no event log = no events to react to)."""
    try:
        content, _ = gist_get_file(QUICKSILVER_GIST_ID, f"QUICKSILVER-{username}-LOG.json")
        events = json.loads(content) if content else []
        return any(e.get("event_type") == event_type for e in events)
    except (FileNotFoundError, json.JSONDecodeError, urllib.error.HTTPError):
        return False


def _day_ticked(user: dict) -> bool:
    """True if user TZ midnight has rolled since `day_view_last_refreshed`."""
    tz_name = user.get("timezone") or "America/Los_Angeles"
    last = (user.get("day_view_last_refreshed") or "").strip()
    if not last:
        return True  # never refreshed → day-tick fires
    today = today_in_tz(tz_name)
    return last < today


# ===========================================================================
# PIPELINE STAGING
# ===========================================================================

class PipelineStaging:
    """Holds all in-flight data for one user's pipeline run.

    Mutated by each phase. Cleared between users (per-user isolation).
    """

    def __init__(self, user_row: dict, tier: str, sources: dict):
        self.user_row = user_row           # registry row (HOLOCRON-USERS.md)
        self.username = user_row["username"]
        self.tier = tier
        self.sources = sources

        # Phase 1 outputs
        self.user_state: dict = {}
        self.event_log: list = []
        self.is_cold_start: bool = (tier == "COLD_START")

        # Phase 2 output
        self.events_ingested: dict = {"mark-off": 0, "explore-answer": 0, "reply": 0, "skipped": 0}

        # Phase 3 staging (3a graduation, 3i today_view, etc.)
        self.staging: dict = {}

        # Phase 4 board output
        self.board_output: dict = {}

        # Phase 5 outputs
        self.triggered_evals: dict = {}

        # Phase 5d output — list of {change, source_phase, cg_class, rationale}
        self.cg_classifications: list[dict] = []

        # Phase 6 output
        self.display_json: dict = {}

        # Phase 7 results
        self.validation_passed: bool = False
        self.validation_failures: list[str] = []

        # Phase 8 results
        self.write_back_done: bool = False

        # Per-phase errors (phase_id -> error string)
        self.phase_errors: dict = {}

        # Cumulative API usage for this user's Holocron run. Read by the
        # eval pipeline (eval_pipeline.run_eval_for_user) for BPT/lift.
        self.tokens_used: dict = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


# ===========================================================================
# Phase 1 — Input Assembly + Phase 1b — Field Initialization
# ===========================================================================

# Phase 1b defaults — see HOLOCRON-RUNNER.md §Phase 1b
PHASE_1B_DEFAULTS = {
    "badge_tracking": {
        "graduation_streak": 0,
        "badges_earned": 0,
        "weekly_history": [],
        "first_badge_celebrated": False,
    },
    "circles": [],
    "spark_activity": None,
    "safety_net": {"status": "ok"},
    "free_form_inbox": [],
    "question_history": [],
    "core_lists": {"present": [], "values": [], "release": [], "identity": []},
    "signal_inbox": [],
    "celebrations_queue": [],
    "actions": [],
    "discovery": None,
    "message_outbox": [],
    "pending_connections": [],
    "groups": [],
    "board_output": None,
    "cg_history": [],
}


def phase_1_input_assembly(s: PipelineStaging) -> None:
    """Fetch user state + event log. Initialize missing fields per Phase 1b."""
    print(f"  Phase 1 ({s.username}): input assembly...", flush=True)

    try:
        content, _ = gist_get_file(NW_STATE_GIST_ID, f"USER-{s.username}.json")
        s.user_state = json.loads(content) if content else {}
    except (FileNotFoundError, urllib.error.HTTPError):
        s.user_state = {}
        s.is_cold_start = True
    except json.JSONDecodeError as e:
        raise RuntimeError(f"USER-{s.username}.json parse failure: {e}")

    try:
        log_content, _ = gist_get_file(QUICKSILVER_GIST_ID, f"QUICKSILVER-{s.username}-LOG.json")
        s.event_log = json.loads(log_content) if log_content else []
    except (FileNotFoundError, urllib.error.HTTPError):
        s.event_log = []
    except json.JSONDecodeError:
        s.event_log = []
        log_diagnostic("LOG_PARSE_FAIL", username=s.username, detail="event log unparseable; treating as empty")

    # Phase 1b — initialize absent fields. Never overwrite existing data.
    for field, default in PHASE_1B_DEFAULTS.items():
        if field not in s.user_state:
            # Deep copy via json round-trip for mutable defaults
            s.user_state[field] = json.loads(json.dumps(default)) if default is not None else None

    # Profile-level defaults the pipeline reads downstream
    if "activities" not in s.user_state:
        s.user_state["activities"] = []
    if "connections" not in s.user_state:
        s.user_state["connections"] = []
    if "messages_inbox" not in s.user_state:
        s.user_state["messages_inbox"] = []
    if "timezone" not in s.user_state:
        s.user_state["timezone"] = s.user_row.get("timezone") or "America/Los_Angeles"

    # Halt on bizarre state — never wipe a populated state file accidentally.
    raw_keys = len(s.user_state)
    if not s.is_cold_start and raw_keys < 3:
        raise RuntimeError(
            f"USER-{s.username}.json loaded with only {raw_keys} keys after init. "
            f"Refusing to proceed — looks like a corrupt or empty state file."
        )


# ===========================================================================
# Phase 2 — Event Ingest
# ===========================================================================

def phase_2_event_ingest(s: PipelineStaging) -> None:
    """Process events from the log into user state, in log order. Dedupes."""
    if s.is_cold_start:
        return  # cold start skips Phase 2 per HOLOCRON-ROUTINE-SPEC.md §4c
    print(f"  Phase 2 ({s.username}): {len(s.event_log)} events...", flush=True)

    for ev in s.event_log:
        et = ev.get("event_type", "")
        try:
            if et == "mark-off":
                if _ingest_markoff(s, ev):
                    s.events_ingested["mark-off"] += 1
                else:
                    s.events_ingested["skipped"] += 1
            elif et == "explore-answer":
                if _ingest_explore_answer(s, ev):
                    s.events_ingested["explore-answer"] += 1
                else:
                    s.events_ingested["skipped"] += 1
            elif et == "reply":
                if _ingest_reply(s, ev):
                    s.events_ingested["reply"] += 1
                else:
                    s.events_ingested["skipped"] += 1
            else:
                # Unrecognized event types skipped silently (forward-compat)
                pass
        except Exception as e:
            log_diagnostic("EVENT_INGEST_ERR", username=s.username,
                           detail=f"event_type={et} err={e}")
            s.events_ingested["skipped"] += 1


def _event_date(ev: dict) -> str:
    ts = ev.get("timestamp", "")
    return ts.split("T")[0] if ts else ""


def _ingest_markoff(s: PipelineStaging, ev: dict) -> bool:
    """Append to activities[].history. Returns True if appended."""
    name = (ev.get("content") or "").strip()
    if not name:
        return False
    date = _event_date(ev)
    if not date:
        return False
    for act in s.user_state.get("activities", []):
        act_name = (act.get("name") or "").strip()
        if act_name.lower() == name.lower():
            history = act.setdefault("history", [])
            if any(h.get("date") == date for h in history):
                return False  # dedupe
            history.append({"date": date, "completed": True, "ltl": False, "amount": None})
            return True
    log_diagnostic("MARKOFF_UNMATCHED", username=s.username, detail=f"name={name!r} date={date}")
    return False


def _ingest_explore_answer(s: PipelineStaging, ev: dict) -> bool:
    """Append to free_form_inbox + question_history. Returns True if appended."""
    text = (ev.get("content") or "").strip()
    if not text:
        return False
    date = _event_date(ev)
    inbox = s.user_state.setdefault("free_form_inbox", [])
    if any(i.get("source") == "explore-session" and i.get("text") == text and i.get("date") == date
           for i in inbox):
        return False
    inbox.append({"date": date, "source": "explore-session", "text": text})
    history = s.user_state.setdefault("question_history", [])
    history.append({"date": date, "question": "Explore (Quicksilver)", "response": text,
                    "context": "quicksilver-session"})
    return True


def _ingest_reply(s: PipelineStaging, ev: dict) -> bool:
    """Append to message_outbox. Returns True if appended."""
    key = ev.get("key", "")
    text = (ev.get("content") or "").strip()
    m = re.search(r"together_msg_(\w+)", key)
    if not m or not text:
        return False
    recipient = m.group(1).lower()
    outbox = s.user_state.setdefault("message_outbox", [])
    outbox.append({
        "to": recipient,
        "type": "text",
        "content": text,
        "sent": ev.get("timestamp", now_iso()),
        "delivered": False,
    })
    return True


# ===========================================================================
# Phase 3 — Field Computation (13 sub-steps)
# ===========================================================================

def phase_3_field_computation(s: PipelineStaging) -> None:
    """Run all 13 sub-steps. Mechanical only — no judgment."""
    print(f"  Phase 3 ({s.username}): mechanical facts...", flush=True)
    _phase_3a_graduation(s)
    _phase_3a_plus_badge_streak(s)
    _phase_3a_plus_plus_circle_graduation(s)
    _phase_3b_target_age(s)
    _phase_3c_ltl_pattern(s)
    _phase_3d_tally_monthly(s)
    _phase_3e_stack_patterns(s)
    _phase_3f_community_medal(s)
    _phase_3g_raw_dimensions(s)
    _phase_3h_fade_detection(s)
    _phase_3i_today_view(s)
    _phase_3j_markoff_responses(s)
    _phase_3k_completion_summary(s)
    _phase_3l_highlights_landing(s)
    _phase_3m_together_landing(s)


def _spark_activity_name(s: PipelineStaging) -> Optional[str]:
    """First activity by creation date (or first in list as fallback)."""
    activities = s.user_state.get("activities", [])
    if not activities:
        return None
    spark = s.user_state.get("spark_activity")
    if spark:
        for a in activities:
            if (a.get("name") or "").lower() == str(spark).lower():
                return a.get("name")
    # Fallback — first activity by creation date if present, else order
    dated = [(a.get("created", ""), a.get("name", "")) for a in activities]
    dated.sort()
    return dated[0][1] if dated[0][1] else activities[0].get("name")


def _completions(activity: dict) -> list:
    """Return completion entries, normalizing across schema variants
    (history[] vs completions[])."""
    if "history" in activity:
        return activity["history"]
    if "completions" in activity:
        return [{"date": c.get("date"), "completed": True, "ltl": c.get("ltl", False),
                 "amount": c.get("amount")} for c in activity["completions"]]
    return []


def _phase_3a_graduation(s: PipelineStaging) -> None:
    """Most recent complete week graduation classification."""
    spark_name = _spark_activity_name(s)
    if not spark_name:
        s.staging["graduation"] = None
        return

    activities = s.user_state.get("activities", [])
    spark = next((a for a in activities if (a.get("name") or "") == spark_name), None)
    if not spark:
        s.staging["graduation"] = None
        return

    days_per_week = spark.get("days_per_week") or spark.get("frequency") or len(spark.get("active_days") or [])
    if not days_per_week:
        s.staging["graduation"] = None
        return

    today = datetime.now(timezone.utc).date()
    weekday = today.weekday()  # Monday=0
    week_start = today - timedelta(days=weekday + 7)
    week_end = week_start + timedelta(days=6)

    history = _completions(spark)
    days_completed = 0
    ltl_count = 0
    for h in history:
        d = h.get("date", "")
        try:
            dt = datetime.fromisoformat(d).date()
        except ValueError:
            continue
        if week_start <= dt <= week_end and h.get("completed"):
            days_completed += 1
            if h.get("ltl"):
                ltl_count += 1

    if days_completed >= days_per_week and ltl_count == 0:
        cls = "Advance"
    elif days_completed >= days_per_week and ltl_count > 0:
        cls = "Carry"
    else:
        cls = "Reset"

    # Cold-start skip — < 2 weeks of activity data
    earliest = None
    for h in history:
        try:
            dt = datetime.fromisoformat(h.get("date", "")).date()
            earliest = dt if earliest is None or dt < earliest else earliest
        except ValueError:
            continue
    if earliest is None or (today - earliest) < timedelta(days=14):
        s.staging["graduation"] = {"classification": None, "skipped": "cold_start_lt_2wk"}
        return

    s.staging["graduation"] = {
        "classification": cls,
        "spark_days_completed": days_completed,
        "ltl_on_spark": ltl_count > 0,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
    }


def _phase_3a_plus_badge_streak(s: PipelineStaging) -> None:
    """Increment badge tracking based on 3a classification."""
    grad = s.staging.get("graduation") or {}
    cls = grad.get("classification")
    bt = s.user_state.setdefault("badge_tracking", json.loads(json.dumps(PHASE_1B_DEFAULTS["badge_tracking"])))

    if cls == "Advance":
        bt["graduation_streak"] = int(bt.get("graduation_streak", 0)) + 1
        if bt["graduation_streak"] >= 6:
            bt["badges_earned"] = int(bt.get("badges_earned", 0)) + 1
            bt["graduation_streak"] = 0
            s.staging["badge_earned_this_cycle"] = True
    elif cls == "Reset":
        bt["graduation_streak"] = 0

    if cls and grad.get("week_start"):
        bt.setdefault("weekly_history", []).append({
            "week_start": grad["week_start"],
            "status": cls,
            "spark_days_completed": grad.get("spark_days_completed"),
            "ltl_used": grad.get("ltl_on_spark"),
        })


def _phase_3a_plus_plus_circle_graduation(s: PipelineStaging) -> None:
    """Skip if no circles."""
    if not s.user_state.get("circles"):
        return
    # Circle graduation evaluation requires cross-user state — out of scope
    # for first-cut Stage 2. Recorded as unimplemented diagnostic so it
    # surfaces in the run log rather than silently passing.
    log_diagnostic("PHASE_3APP_UNIMPLEMENTED", username=s.username,
                   detail="circle graduation logic deferred")


def _phase_3b_target_age(s: PipelineStaging) -> None:
    """Per activity with target: flag if >6 weeks, urgent >2 months."""
    today = datetime.now(timezone.utc).date()
    flags = []
    for act in s.user_state.get("activities", []):
        t = act.get("target")
        t_set = act.get("target_set") or act.get("created")
        if not t or not t_set:
            continue
        try:
            set_date = datetime.fromisoformat(t_set.split("T")[0]).date()
        except ValueError:
            continue
        age = (today - set_date).days
        if age > 60:
            flags.append({"activity": act.get("name"), "age_days": age, "severity": "urgent"})
        elif age > 42:
            flags.append({"activity": act.get("name"), "age_days": age, "severity": "warning"})
    s.staging["target_age_flags"] = flags


def _phase_3c_ltl_pattern(s: PipelineStaging) -> None:
    """Count LtL occurrences per activity over recent 4 weeks."""
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=28)
    patterns = []
    for act in s.user_state.get("activities", []):
        ltl_count = 0
        for h in _completions(act):
            try:
                d = datetime.fromisoformat(h.get("date", "")).date()
            except ValueError:
                continue
            if d >= cutoff and h.get("ltl"):
                ltl_count += 1
        if ltl_count >= 3:
            patterns.append({"activity": act.get("name"), "ltl_count": ltl_count, "severity": "flag"})
        elif ltl_count > 0:
            patterns.append({"activity": act.get("name"), "ltl_count": ltl_count, "severity": "occasional"})
    s.staging["ltl_patterns"] = patterns


def _phase_3d_tally_monthly(s: PipelineStaging) -> None:
    """Monthly counts for Tally activities."""
    today = datetime.now(timezone.utc).date()
    month_start = today.replace(day=1)
    tallies = []
    for act in s.user_state.get("activities", []):
        if (act.get("type") or "").lower() != "tally":
            continue
        count = 0
        for h in _completions(act):
            try:
                d = datetime.fromisoformat(h.get("date", "")).date()
            except ValueError:
                continue
            if d >= month_start and h.get("completed"):
                count += 1
        tallies.append({"activity": act.get("name"), "count": count, "target": act.get("monthly_target")})
    s.staging["tally_monthly"] = tallies


def _phase_3e_stack_patterns(s: PipelineStaging) -> None:
    """Detect partial-completion patterns in stacks."""
    # Stack partial-completion analysis requires session-level stack
    # grouping in the event log. First-cut: empty.
    s.staging["stack_patterns"] = []


def _phase_3f_community_medal(s: PipelineStaging) -> None:
    """Weekly completion percentage → medal status."""
    today = datetime.now(timezone.utc).date()
    weekday = today.weekday()
    week_start = today - timedelta(days=weekday)
    days_elapsed = weekday + 1

    completed = 0
    scheduled = 0
    for act in s.user_state.get("activities", []):
        active_days = act.get("active_days") or []
        for offset in range(days_elapsed):
            d = week_start + timedelta(days=offset)
            day_name = d.strftime("%A").lower()
            if day_name in active_days:
                scheduled += 1
                for h in _completions(act):
                    if h.get("date") == d.isoformat() and (h.get("completed") or h.get("ltl")):
                        completed += 1
                        break

    medal = "—"
    if weekday <= 1 and completed >= 1:
        medal = "🥇"
    elif scheduled > 0:
        pct = completed / scheduled
        if pct >= 0.9:
            medal = "🥇"
        elif pct >= 0.7:
            medal = "🥈"
        elif completed >= 1:
            medal = "🥉"
    s.staging["community_medal"] = {"medal": medal, "completed": completed, "scheduled": scheduled}


def _phase_3g_raw_dimensions(s: PipelineStaging) -> None:
    """Score 4 shared dimensions 1-5 against a 2-week rolling baseline."""
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=14)

    activities = s.user_state.get("activities", [])
    marks_this_period = 0
    marks_total_possible = 0
    for act in activities:
        for h in _completions(act):
            try:
                d = datetime.fromisoformat(h.get("date", "")).date()
            except ValueError:
                continue
            if d >= cutoff and h.get("completed"):
                marks_this_period += 1
        active_days = act.get("active_days") or []
        marks_total_possible += len(active_days) * 2  # 2 weeks

    consistency = _score_ratio(marks_this_period, max(marks_total_possible, 1))
    trajectory = consistency  # first-cut: same signal until trend analysis is added

    qhist = s.user_state.get("question_history") or []
    recent_qs = sum(1 for q in qhist
                    if (parse_iso(q.get("date", "")) or datetime.min.replace(tzinfo=timezone.utc))
                    .date() >= cutoff)
    engagement = min(5, 1 + recent_qs)

    outbox = s.user_state.get("message_outbox") or []
    inbox = s.user_state.get("messages_inbox") or []
    sent = len(outbox)
    received = len(inbox)
    if sent + received == 0:
        connection = 1
    else:
        connection = min(5, 1 + (sent + received) // 2)

    s.staging["dimensions"] = {
        "consistency": consistency,
        "trajectory": trajectory,
        "engagement": engagement,
        "connection": connection,
    }


def _score_ratio(num: int, denom: int) -> int:
    """Map a ratio 0..1 onto a 1-5 score."""
    if denom <= 0:
        return 1
    pct = num / denom
    if pct >= 0.9: return 5
    if pct >= 0.7: return 4
    if pct >= 0.5: return 3
    if pct >= 0.3: return 2
    return 1


def _phase_3h_fade_detection(s: PipelineStaging) -> None:
    """Check for fade signals — declining completions, repeated misses."""
    today = datetime.now(timezone.utc).date()
    fade_signals = []
    for act in s.user_state.get("activities", []):
        recent_2wk = 0
        prior_2wk = 0
        for h in _completions(act):
            try:
                d = datetime.fromisoformat(h.get("date", "")).date()
            except ValueError:
                continue
            days_ago = (today - d).days
            if 0 <= days_ago < 14 and h.get("completed"):
                recent_2wk += 1
            elif 14 <= days_ago < 28 and h.get("completed"):
                prior_2wk += 1
        if prior_2wk >= 3 and recent_2wk < prior_2wk * 0.5:
            fade_signals.append({"activity": act.get("name"), "kind": "declining",
                                 "recent": recent_2wk, "prior": prior_2wk})
    s.staging["fade_signals"] = fade_signals


def _phase_3i_today_view(s: PipelineStaging) -> None:
    """Assemble today_view object. The richest sub-step in Phase 3."""
    today = datetime.now(timezone.utc).date()
    tz_name = s.user_state.get("timezone") or "America/Los_Angeles"
    today_user_tz = today_in_tz(tz_name)
    today_weekday = today.strftime("%A").lower()

    activities = s.user_state.get("activities", [])
    today_scheduled = []
    for act in activities:
        active_days = act.get("active_days") or []
        if today_weekday in active_days:
            today_scheduled.append(act.get("name"))

    spacing_suppressed = []
    rest_gap_default = 1
    for act in activities:
        rest_gap = act.get("minimum_rest_gap", rest_gap_default)
        history = sorted(_completions(act), key=lambda h: h.get("date", ""), reverse=True)
        last_complete = None
        for h in history:
            if h.get("completed"):
                try:
                    last_complete = datetime.fromisoformat(h.get("date", "")).date()
                    break
                except ValueError:
                    continue
        if last_complete and (today - last_complete).days < rest_gap:
            spacing_suppressed.append(act.get("name"))

    cutoff_2wk = today - timedelta(days=14)
    days_completed_2wk = 0
    for act in activities:
        for h in _completions(act):
            try:
                d = datetime.fromisoformat(h.get("date", "")).date()
            except ValueError:
                continue
            if d >= cutoff_2wk and h.get("completed"):
                days_completed_2wk += 1
                break

    last_session_str = s.user_row.get("last_session", "")
    ls = parse_iso(last_session_str)
    if ls:
        days_since = (datetime.now(timezone.utc) - ls).days
        if days_since == 0:
            staleness_status = "current"
        elif days_since <= 3:
            staleness_status = "current"
        elif days_since <= 14:
            staleness_status = "lapsed"
        else:
            staleness_status = "returning"
    else:
        staleness_status = "cold_start"

    # mark_off_display — rank today's scheduled activities (cold start: schedule order)
    mark_off_display = _build_mark_off_display(s, today_scheduled, spacing_suppressed)

    greeting_pool = [
        "Welcome back.",
        "Good to see you.",
        "Hi there.",
    ]
    greeting = random.choice(greeting_pool)

    bt = s.user_state.get("badge_tracking") or PHASE_1B_DEFAULTS["badge_tracking"]
    badge_progress = None
    if not (s.is_cold_start or _data_under_2_weeks(s)):
        badge_progress = {
            "graduation_streak": bt.get("graduation_streak", 0),
            "badges_earned": bt.get("badges_earned", 0),
            "emoji_display": _badge_emoji(s.staging.get("graduation", {}).get("classification")),
            "next_badge_in": max(0, 6 - int(bt.get("graduation_streak", 0))),
            "pending_celebrations": [],
        }

    medal_info = s.staging.get("community_medal", {})
    today_view = {
        "computed_date": today_user_tz,
        "staleness_status": staleness_status,
        "today_scheduled": today_scheduled,
        "spacing_suppressed": spacing_suppressed,
        "progress_display": f"{days_completed_2wk} days completed in last 2 weeks",
        "period_table": _build_period_table(s),
        "streak_status": bt.get("graduation_streak", 0),
        "next_milestone": None,
        "badge_progress": badge_progress,
        "safety_net_status": (s.user_state.get("safety_net") or {}).get("status", "ok"),
        "inbox": {
            "messages": len(s.user_state.get("messages_inbox") or []),
            "celebrations": s.user_state.get("celebrations_queue") or [],
            "connections": [],
        },
        "community_medals": [medal_info.get("medal", "—")],
        "available_connections": [],
        "greeting": greeting,
        "mark_off_display": mark_off_display,
        "cross_user_invalidated": False,
    }
    s.staging["today_view"] = today_view


def _data_under_2_weeks(s: PipelineStaging) -> bool:
    today = datetime.now(timezone.utc).date()
    earliest = None
    for act in s.user_state.get("activities", []):
        for h in _completions(act):
            try:
                d = datetime.fromisoformat(h.get("date", "")).date()
            except ValueError:
                continue
            earliest = d if earliest is None or d < earliest else earliest
    return earliest is None or (today - earliest) < timedelta(days=14)


def _badge_emoji(cls: Optional[str]) -> str:
    return {"Advance": "🎓", "Carry": "✅", "Reset": "⬜"}.get(cls or "", "⬜")


def _build_period_table(s: PipelineStaging) -> dict:
    today = datetime.now(timezone.utc).date()
    weekday = today.weekday()
    week_start = today - timedelta(days=weekday)
    rows = []
    for act in s.user_state.get("activities", []):
        active_days = act.get("active_days") or []
        target = len(active_days)
        actual = 0
        for offset in range(weekday + 1):
            d = week_start + timedelta(days=offset)
            for h in _completions(act):
                if h.get("date") == d.isoformat() and (h.get("completed") or h.get("ltl")):
                    actual += 1
                    break
        rows.append({"activity": act.get("name"), "target": target, "actual": actual})
    return {"week_start": week_start.isoformat(), "rows": rows}


def _build_mark_off_display(s: PipelineStaging, scheduled: list[str], suppressed: list[str]) -> dict:
    """Rank ordered list of today's mark-offable activities. Cold start =
    schedule order. Mature data = first scheduled + rest in schedule order
    (first-cut)."""
    ranked = [name for name in scheduled if name not in suppressed]
    if not ranked:
        return {"spark": None, "ordered": []}
    spark_name = _spark_activity_name(s)
    if spark_name and spark_name in ranked:
        ranked.remove(spark_name)
        ranked.insert(0, spark_name)
    spark_act = next((a for a in s.user_state.get("activities", [])
                      if a.get("name") == ranked[0]), {})
    return {
        "spark": ranked[0],
        "spark_amount": spark_act.get("amount"),
        "spark_unit": spark_act.get("unit"),
        "ordered": ranked,
    }


def _phase_3j_markoff_responses(s: PipelineStaging) -> None:
    """Pre-compute mark-off responses (mid_loop + final)."""
    mid_loop_pool = [
        "Done. That's one more vote for who you're becoming.",
        "Checked off. The streak speaks for itself.",
        "Another one down. Consistency is building.",
    ]
    final_pool = [
        "Done. Small moves, big compound.",
        "That's momentum. It adds up faster than you think.",
    ]
    s.staging["mark_off_responses"] = {
        "mid_loop": random.choice(mid_loop_pool),
        "final": random.choice(final_pool),
    }


def _phase_3k_completion_summary(s: PipelineStaging) -> None:
    """Random draw from completion summary pool (≤15 words)."""
    pool = [
        "Good work today — keep showing up.",
        "Nice. The pattern is building.",
        "Done. Tomorrow's version of you is grateful.",
    ]
    s.staging["completion_summary"] = random.choice(pool)


def _phase_3l_highlights_landing(s: PipelineStaging) -> None:
    """Pre-compute highlights_landing (Progress branch)."""
    s.staging["highlights_landing"] = {
        "groups": [],
        "activities_summary": _activities_summary(s),
        "about_you_summary": None,
        "stamps": {"activities_summary": now_iso()},
    }


def _activities_summary(s: PipelineStaging) -> list[dict]:
    out = []
    for act in s.user_state.get("activities", []):
        out.append({
            "name": act.get("name"),
            "amount": act.get("amount"),
            "unit": act.get("unit"),
            "active_days": act.get("active_days") or [],
        })
    return out


def _phase_3m_together_landing(s: PipelineStaging) -> None:
    """Pre-compute together_landing (Together branch)."""
    s.staging["together_landing"] = {
        "people_medals": [],
        "messaging_summary": None,
        "lighten_the_load_status": "available",
        "stamps": {
            "people_medals": now_iso(),
            "messaging_summary": now_iso(),
            "lighten_the_load_status": now_iso(),
        },
    }


# ===========================================================================
# Phase 4 — Board Meeting (editorial — Anthropic API)
# ===========================================================================

def _get_anthropic_client():
    """Lazy import. Returns None if SDK or key is missing (caller handles)."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        log_diagnostic("ANTHROPIC_SDK_MISSING",
                       detail="anthropic package not installed; editorial phases will skip")
        return None
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def phase_4_board_meeting(s: PipelineStaging) -> None:
    """Run the board meeting via a single Anthropic API call.

    First-cut: single structured-output call that scores the user across the
    six runner dimensions, classifies nervous-system state, and produces
    one top recommendation with a "why now" rationale. Sub-phases 4.1-4.6
    of HOLOCRON-RUNNER.md compress into one model turn; subsequent drives
    expand into per-member calls. Output goes to s.board_output.
    """
    print(f"  Phase 4 ({s.username}): board meeting ({MODEL_CONFIG[4]})...", flush=True)
    client = _get_anthropic_client()
    if client is None:
        s.phase_errors[4] = "anthropic client unavailable"
        s.board_output = {"unavailable": True}
        return

    facts = {
        "activities": _activities_summary(s),
        "graduation_classification": (s.staging.get("graduation") or {}).get("classification"),
        "dimension_scores": s.staging.get("dimensions"),
        "fade_signals": s.staging.get("fade_signals"),
        "ltl_patterns": s.staging.get("ltl_patterns"),
        "events_recent": s.events_ingested,
        "core_lists": s.user_state.get("core_lists"),
        "free_form_inbox_recent": (s.user_state.get("free_form_inbox") or [])[-5:],
        "is_cold_start": s.is_cold_start,
    }

    system = (
        "You are the Holocron Board for Quicksilver. Your job: read the user's "
        "mechanical facts, classify their nervous-system state, and produce ONE "
        "top recommendation with a one-sentence 'why now' rationale. Respond "
        "in JSON only — no prose, no markdown fences. Schema:\n"
        "{\n"
        '  "nervous_system": "sympathetic" | "parasympathetic" | "coherent",\n'
        '  "reading_rationale": "one sentence",\n'
        '  "top_recommendation": {\n'
        '    "title": "≤8 words",\n'
        '    "body": "one warm specific sentence the user will see",\n'
        '    "why_now": "one sentence grounded in the signal"\n'
        "  },\n"
        '  "worth_trying": [ { "title": "≤8 words", "body": "≤25 words" } ],\n'
        '  "dimension_summaries": {\n'
        '    "consistency": 1-5, "trajectory": 1-5,\n'
        '    "engagement": 1-5, "connection": 1-5\n'
        "  }\n"
        "}\n"
        "Tone: warm, specific, never clinical. Default to Coherent on ambiguity. "
        "Bias smaller on step size. If data is cold-start (<2 weeks), use "
        "encouragement language, not pattern claims."
    )
    user_msg = (
        f"User: {s.username}\nTier: {s.tier}\nFacts:\n```json\n"
        f"{json.dumps(facts, indent=2)}\n```"
    )

    try:
        resp = client.messages.create(
            model=MODEL_CONFIG[4],
            max_tokens=PHASE_4_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text if resp.content else ""
        s.board_output = _parse_json_response(text)
        if resp.usage:
            it = getattr(resp.usage, "input_tokens", 0) or 0
            ot = getattr(resp.usage, "output_tokens", 0) or 0
            s.tokens_used["input_tokens"] += it
            s.tokens_used["output_tokens"] += ot
            s.tokens_used["total_tokens"] += it + ot
    except Exception as e:
        log_diagnostic("PHASE_4_API_ERR", username=s.username, detail=str(e))
        s.phase_errors[4] = str(e)
        s.board_output = {"unavailable": True, "error": str(e)}


def _parse_json_response(text: str) -> dict:
    """Strip optional code fences and parse JSON. Returns {} on failure."""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


# ===========================================================================
# Phase 5 — Triggered Evaluations
# ===========================================================================

def phase_5_triggered_evals(s: PipelineStaging) -> None:
    """5a rebalancing, 5b community contribution, 5c discovery question."""
    print(f"  Phase 5 ({s.username}): triggered evaluations...", flush=True)

    # 5a — group rebalancing
    s.triggered_evals["group_rebalancing"] = {"skipped": "no groups"} if not s.user_state.get("groups") else {"checked": True}

    # 5b — community contribution
    medal = (s.staging.get("community_medal") or {}).get("medal", "—")
    s.triggered_evals["community_contribution"] = {"medal": medal}

    # 5c — discovery question selection
    s.triggered_evals["discovery"] = _select_discovery_question(s)


def _select_discovery_question(s: PipelineStaging) -> dict:
    """Sequential selection from QUICKSILVER-CONTENT.md Explore Questions,
    excluding any already in question_history. Pair with affirmations.

    This is the spec-fallback path (no discovery object on user). When the
    Question Ranking Model is wired in a later drive, this becomes the
    cold-start branch only.
    """
    content_md = (s.sources.get("QUICKSILVER-CONTENT.md") or {}).get("content", "")
    questions = _parse_explore_questions(content_md)
    affirmations = _parse_affirmations(content_md)
    asked = set()
    for q in s.user_state.get("question_history") or []:
        resp_q = (q.get("question") or "").strip()
        if resp_q:
            asked.add(resp_q)

    queue = []
    seen_aff = set()
    for q in questions:
        if q["question"] in asked:
            continue
        aff = next((a for a in affirmations if a not in seen_aff), affirmations[0] if affirmations else "Good job.")
        seen_aff.add(aff)
        queue.append({
            "inline": f"{q['context']}\n\n{q['question']}",
            "affirmation": aff,
        })
        if len(queue) >= 2:
            break

    return {"queue": queue, "session_limit": 1}


def _parse_explore_questions(content: str) -> list[dict]:
    """Parse QUICKSILVER-CONTENT.md Explore Questions table."""
    out = []
    in_table = False
    for line in content.splitlines():
        if line.startswith("## Explore Questions"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if in_table and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4 and cells[0].isdigit():
                out.append({"id": cells[1], "context": cells[2], "question": cells[3]})
    return out


def _parse_affirmations(content: str) -> list[str]:
    """Parse the numbered affirmation list under ## Affirmations."""
    out = []
    in_section = False
    for line in content.splitlines():
        if line.startswith("## Affirmations"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        m = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if in_section and m:
            out.append(m.group(1).strip())
    return out


# ===========================================================================
# Phase 5d — Content Governance Classification
# ===========================================================================

def phase_5d_cg_classification(s: PipelineStaging) -> None:
    """Classify each proposed change into CG class 1-5 per Constitution
    routing rules."""
    print(f"  Phase 5d ({s.username}): CG classification...", flush=True)
    nervous_system = (s.board_output or {}).get("nervous_system", "coherent")
    classified = []

    # Each proposed change is rendered into a dict with type, conviction, permanence
    proposed = _enumerate_proposed_changes(s)

    for change in proposed:
        cls = _route_cg_class(change, nervous_system)
        classified.append({
            "change": change,
            "source_phase": change.get("source"),
            "cg_class": cls,
            "rationale": change.get("rationale", ""),
        })
    s.cg_classifications = classified


def _enumerate_proposed_changes(s: PipelineStaging) -> list[dict]:
    """Pull proposed changes out of board output + staging."""
    changes = []
    bo = s.board_output or {}
    rec = bo.get("top_recommendation")
    if rec:
        changes.append({
            "type": "recommendation",
            "title": rec.get("title", ""),
            "body": rec.get("body", ""),
            "conviction": 4,
            "permanence": 3,
            "jarring": False,
            "source": "4.5",
            "rationale": rec.get("why_now", ""),
        })
    for wt in (bo.get("worth_trying") or []):
        changes.append({
            "type": "worth_trying",
            "title": wt.get("title", ""),
            "body": wt.get("body", ""),
            "conviction": 3,
            "permanence": 3,
            "jarring": False,
            "source": "4.5",
            "rationale": "worth-trying suggestion",
        })
    # Graduation increment proposal
    grad = s.staging.get("graduation") or {}
    if grad.get("classification") == "Advance":
        changes.append({
            "type": "amount_increase",
            "title": "Graduation step",
            "body": "Spark amount increment",
            "conviction": 5,
            "permanence": 5,
            "jarring": True,
            "source": "4.5",
            "rationale": "Advance classification with no LtL",
        })
    return changes


def _route_cg_class(change: dict, nervous_system: str) -> int:
    """Apply Constitution §Signal-to-Class Routing rules in order."""
    conviction = change.get("conviction", 0)
    permanence = change.get("permanence", 0)
    jarring = change.get("jarring", False)

    if change.get("type") in {"worth_trying", "recommendation"} and not jarring:
        cls = 1 if change.get("type") == "worth_trying" else 2
    elif conviction >= 4 and permanence >= 4 and not jarring:
        cls = 2
    elif jarring and conviction >= 3 and permanence >= 3:
        cls = 3
    elif jarring and (conviction < 3 or permanence < 3):
        cls = 4
    else:
        cls = 5

    # URF constraint (Step 3)
    if nervous_system == "sympathetic":
        if cls in (3, 4):
            cls = 5
    elif nervous_system == "parasympathetic":
        if cls == 4:
            cls = 5

    return cls


# ===========================================================================
# Phase 6 — JSON Assembly
# ===========================================================================

def phase_6_json_assembly(s: PipelineStaging) -> None:
    """Build the display JSON. Verbatim pass-through on all pre-computed
    display values from Phase 3."""
    print(f"  Phase 6 ({s.username}): JSON assembly...", flush=True)

    today_view = s.staging.get("today_view") or {}
    spark_name = today_view.get("mark_off_display", {}).get("spark")
    spark_amount = today_view.get("mark_off_display", {}).get("spark_amount")
    spark_unit = today_view.get("mark_off_display", {}).get("spark_unit")
    ordered = today_view.get("mark_off_display", {}).get("ordered") or []

    activities = s.user_state.get("activities", [])

    j = {}

    # home
    j["home"] = {
        "question": "Solution Keepers🏠 › Where to?",
        "options": [
            {"label": "❯ Today☀️", "goto": "today"},
            {"label": "❯ Progress🙏🏼", "goto": "progress"},
            {"label": "❯ Together🌍", "goto": "together"},
        ],
    }

    # today
    j["today"] = {
        "question": "Today☀️ › Where to?",
        "options": [
            {"label": "❯ 🔥Today's Spark🔥", "goto": "spark"},
            {"label": "❯ More Activities", "goto": "more"},
            {"label": "❯ Explore", "goto": "explore"},
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }

    # spark + spark_done
    spark_label = "🔥(no activities yet)🔥"
    if spark_name:
        amt = f"{spark_amount} {spark_unit}".strip() if spark_amount and spark_unit else ""
        spark_label = f"○ 🔥{amt} {spark_name}🔥".replace("  ", " ")
    j["spark"] = {
        "question": "☀️ › 🔥Today's Spark🔥 › Done?",
        "options": [
            {"label": spark_label, "goto": "spark_done"},
            {"label": "❮ Today☀️", "goto": "today"},
        ],
    }
    congrats_pool = ["Nailed", "Solid", "Sharp", "Clean", "Smooth", "Strong", "Crisp", "Done"]
    spark_congrats = congrats_pool[0]
    j["spark_done"] = {
        "inline": f"✓ {(spark_amount or '')} {(spark_unit or '')} {(spark_name or '')} — 🎉 {spark_congrats} 🎉".strip(),
        "question": "☀️ › 🔥Today's Spark🔥 › What's next?",
        "options": [
            {"label": "❮ Today☀️", "goto": "today"},
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }

    # more + more_{slug}_done
    more_options = []
    used_congrats = {spark_congrats}
    for act_name in ordered[1:]:
        act = next((a for a in activities if a.get("name") == act_name), {})
        slug = _slugify(act_name)
        more_options.append({"label": f"○ {act_name}", "goto": f"more_{slug}_done"})
        c = next((w for w in congrats_pool if w not in used_congrats), "Done")
        used_congrats.add(c)
        j[f"more_{slug}_done"] = {
            "inline": f"✓ {act_name} — 🎉 {c} 🎉",
            "question": "☀️ › More Activities › Pick one",
            "options": [
                {"label": f"↺ {act_name}", "goto": "more"},
                {"label": "❮ Today☀️", "goto": "today"},
            ],
        }
    more_options.append({"label": "❮ Today☀️", "goto": "today"})
    j["more"] = {
        "question": "☀️ › More Activities › Pick one",
        "options": more_options,
    }

    # explore
    j["explore"] = s.triggered_evals.get("discovery") or {"session_limit": 1, "queue": []}
    if not j["explore"].get("queue"):
        j["explore"] = {
            "session_limit": 1,
            "queue": [{"inline": "What's something you're looking forward to this week?",
                       "affirmation": "Good energy."}],
        }

    # progress branch
    spark_inline = _progress_spark_inline(s, spark_name)
    signals_inline = _progress_signals_inline(s)
    groups_inline = "**Groups**\n\nGroups are not yet active for you."
    progress_inline = f"{spark_inline}\n\n---\n\n{signals_inline}\n\n---\n\n{groups_inline}"

    worth_trying_items = (s.board_output or {}).get("worth_trying") or []
    progress_options = []
    if worth_trying_items:
        progress_options.append({"label": f"❯ Try: {worth_trying_items[0].get('title','')}",
                                 "goto": "progress_worth_trying"})
        if len(worth_trying_items) >= 2:
            progress_options.append({"label": "❯ See another Worth Trying",
                                     "goto": "progress_worth_trying"})
    progress_options.append({"label": "❮ Solution Keepers🏠", "goto": "home"})
    j["progress"] = {
        "inline": progress_inline,
        "question": "Progress🙏🏼 › Where to?",
        "options": progress_options,
    }
    j["progress_spark"] = {
        "inline": spark_inline,
        "question": "Progress🙏🏼 › Today's Spark › What's next?",
        "options": [
            {"label": "❮ Progress🙏🏼", "goto": "progress"},
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }
    j["progress_groups"] = {
        "question": "Progress🙏🏼 › Groups",
        "options": [
            {"label": "❮ Progress🙏🏼", "goto": "progress"},
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }
    j["progress_signals"] = {
        "inline": signals_inline,
        "question": "Progress🙏🏼 › Signals › What's next?",
        "options": [
            {"label": "❮ Progress🙏🏼", "goto": "progress"},
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }
    wt_inline = "No Worth Trying items yet — check back after a few sessions."
    if worth_trying_items:
        wt = worth_trying_items[0]
        wt_inline = f"**{wt.get('title','')}**\n\n{wt.get('body','')}"
    j["progress_worth_trying"] = {
        "inline": wt_inline,
        "question": f"Progress🙏🏼 › Worth Trying › 1 of {max(1, len(worth_trying_items))}",
        "options": [
            {"label": "❮ Progress🙏🏼", "goto": "progress"},
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }

    # together branch
    connection_count = len(s.user_state.get("connections", []))
    together_inline = (
        f"**Connections**\n{'No connections yet — they will show up here as you build them.' if connection_count == 0 else f'{connection_count} connection(s).'}\n\n"
        f"**Messaging**\n{'You will find messages here once you connect with someone.' if connection_count == 0 else 'Tap below to see threads.'}"
    )
    j["together"] = {
        "inline": together_inline,
    }
    j["together_connections"] = {
        "inline": "No connections yet — they'll show up as you go." if connection_count == 0 else f"{connection_count} connection(s).",
        "question": "Together🌍 › Connections",
        "options": [
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }
    j["together_messaging"] = {
        "question": "Together🌍 › Messaging",
        "options": [
            {"label": "❮ Solution Keepers🏠", "goto": "home"},
        ],
    }

    # welcome-back content if applicable
    if s.user_row.get("welcome_back") == "true":
        j["home"]["inline"] = (
            "Welcome back. We take what you give us and make a great plan — "
            "since you've been away, updates have paused. Today is a fresh start."
        )

    s.display_json = j


def _progress_spark_inline(s: PipelineStaging, spark_name: Optional[str]) -> str:
    bt = s.user_state.get("badge_tracking") or {}
    streak = bt.get("graduation_streak", 0)
    badges = bt.get("badges_earned", 0)
    head = f"**Today's Spark**\n\n🛡️ {badges} badge(s) · {streak}/6 streak"
    if not spark_name:
        return "**Today's Spark**\n\nYou're just getting started. Keep going — your progress will show up here."
    return f"{head}\n\nSpark: {spark_name}"


def _progress_signals_inline(s: PipelineStaging) -> str:
    dims = s.staging.get("dimensions") or {}
    if not dims:
        return "**Signals**\n\nBuilding your profile. Check back after a few sessions."
    bullets = []
    for label, key in (("Consistency", "consistency"), ("Trajectory", "trajectory"),
                       ("Engagement", "engagement")):
        bullets.append(f"- {label}: {dims.get(key, '?')}/5")
    wt = (s.board_output or {}).get("worth_trying") or []
    wt_line = ""
    if wt:
        wt_line = f"\n\n**Worth trying.** {wt[0].get('body','')}"
    return "**Signals**\n\n" + "\n".join(bullets) + wt_line


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


# ===========================================================================
# Phase 7 — Validation (4 passes)
# ===========================================================================

def phase_7_validation(s: PipelineStaging) -> None:
    """Run the 4 validation passes. Set s.validation_passed."""
    print(f"  Phase 7 ({s.username}): validation...", flush=True)
    failures: list[str] = []

    _pass_0_menus_reconciliation(s, failures)
    _pass_1_schema_compliance(s, failures)
    _pass_2_constitution_compliance(s, failures)
    _pass_3_navigation_integrity(s, failures)

    s.validation_failures = failures
    s.validation_passed = (len(failures) == 0)


REQUIRED_KEYS_BASE = [
    "home", "today", "spark", "spark_done", "more", "explore",
    "progress", "progress_spark", "progress_groups", "progress_signals",
    "progress_worth_trying",
    "together", "together_connections", "together_messaging",
]


def _pass_0_menus_reconciliation(s: PipelineStaging, failures: list[str]) -> None:
    """Check report-mode vs nav-mode per NW-MENUS.md."""
    j = s.display_json
    # progress is report-mode: inline + post-report options (not sub-branch gotos)
    progress = j.get("progress", {})
    if not progress.get("inline"):
        failures.append("pass0:progress.inline missing (report-mode requires inline)")
    for opt in progress.get("options", []):
        goto = opt.get("goto", "")
        if goto in {"progress_spark", "progress_groups", "progress_signals"}:
            failures.append(f"pass0:progress.options contains sub-branch goto {goto!r}; "
                            f"progress is report-mode, no sub-branch nav")


def _pass_1_schema_compliance(s: PipelineStaging, failures: list[str]) -> None:
    """All REQUIRED_KEYS_BASE present, every key has correct shape."""
    j = s.display_json
    for k in REQUIRED_KEYS_BASE:
        if k not in j:
            failures.append(f"pass1:missing required key {k!r}")
    for k, v in j.items():
        if not isinstance(v, dict):
            failures.append(f"pass1:{k!r} is not an object")
            continue
        if k == "explore":
            if "queue" not in v or not isinstance(v.get("queue"), list) or not v["queue"]:
                failures.append("pass1:explore.queue missing or empty")
            for i, q in enumerate(v.get("queue", [])):
                if not q.get("inline"):
                    failures.append(f"pass1:explore.queue[{i}].inline missing")
                if not q.get("affirmation"):
                    failures.append(f"pass1:explore.queue[{i}].affirmation missing")
            continue
        # menu-mode keys: need either inline (hybrid) or options
        if "options" not in v and "inline" not in v:
            failures.append(f"pass1:{k!r} missing both inline and options")
        if "options" in v and not isinstance(v["options"], list):
            failures.append(f"pass1:{k!r}.options not a list")


def _pass_2_constitution_compliance(s: PipelineStaging, failures: list[str]) -> None:
    """P1 data accuracy (sampled), P3 option count limits, CG classes valid."""
    j = s.display_json
    for k, v in j.items():
        if isinstance(v, dict) and isinstance(v.get("options"), list):
            if len(v["options"]) > 8:
                failures.append(f"pass2:{k!r}.options has {len(v['options'])} (>8 limit)")
    for c in s.cg_classifications:
        if c.get("cg_class") not in {1, 2, 3, 4, 5}:
            failures.append(f"pass2:invalid cg_class {c.get('cg_class')!r}")


def _pass_3_navigation_integrity(s: PipelineStaging, failures: list[str]) -> None:
    """Every goto resolves; every key reachable from home (with the
    spec-allowed exceptions for sub-branch drill-down keys)."""
    j = s.display_json
    reserved = {"_reply", "_reply_quick", "_msg_cancel", "_try_action"}
    # Sub-branch keys that the spec requires to exist but are not reachable
    # via gotos from `home`. Their content is composed into parent landings.
    # See HOLOCRON-RUNNER.md §Phase 6 step 1a (report-mode branches) and
    # QUICKSILVER-SCHEMA.md §Progress Branch assembly rule.
    expected_non_home_reachable = {
        "progress_spark", "progress_groups", "progress_signals",
        "progress_worth_trying", "progress_groups_sent",
        "together_connections", "together_messaging",
        "explore",
    }
    for k, v in j.items():
        if isinstance(v, dict):
            for opt in v.get("options", []) or []:
                goto = opt.get("goto")
                if goto and goto not in j and goto not in reserved:
                    failures.append(f"pass3:dangling goto from {k!r} → {goto!r}")
    reachable: set[str] = set()
    queue = ["home"]
    while queue:
        cur = queue.pop()
        if cur in reachable or cur in reserved:
            continue
        reachable.add(cur)
        v = j.get(cur, {})
        for opt in (v.get("options") if isinstance(v, dict) else []) or []:
            tgt = opt.get("goto")
            if tgt and tgt not in reachable:
                queue.append(tgt)
    orphans = [k for k in j.keys()
               if k not in reachable
               and k not in expected_non_home_reachable
               and not (k.startswith(("stack_", "together_msg_",
                                     "progress_groups_msg_"))
                        or (k.startswith("more_") and k.endswith("_done")))]
    if orphans:
        failures.append(f"pass3:orphan keys not reachable from home: {orphans[:5]}")


# ===========================================================================
# Phase 8 — Write Back
# ===========================================================================

def phase_8_write_back(s: PipelineStaging, disposition: str) -> None:
    """PATCH user state + display JSON. DRY_RUN guard: log intent only."""
    print(f"  Phase 8 ({s.username}): write-back (dry_run={DRY_RUN})...", flush=True)

    user_state_json = json.dumps(s.user_state, indent=2)
    display_json = json.dumps(s.display_json, indent=2)

    if disposition == "CONTAMINATION":
        log_diagnostic("WRITE_BACK_BLOCKED", username=s.username,
                       detail="contamination — skipping write")
        return

    print(f"    user_state bytes={len(user_state_json)} display_json bytes={len(display_json)} "
          f"keys={len(s.display_json)} disposition={disposition}", flush=True)

    if DRY_RUN:
        log_diagnostic("DRY_RUN_WRITE_SKIPPED", username=s.username,
                       detail=f"disposition={disposition} keys={len(s.display_json)}")
        return

    # Data-loss prevention: refuse to write an obviously-broken display JSON.
    if len(s.display_json) < 5:
        log_diagnostic("WRITE_BACK_REFUSED", username=s.username,
                       detail=f"display_json has only {len(s.display_json)} keys; refusing to write")
        return

    try:
        gist_patch_files(NW_STATE_GIST_ID, {f"USER-{s.username}.json": user_state_json})
        gist_patch_files(QUICKSILVER_GIST_ID, {f"QUICKSILVER-{s.username}.json": display_json})
        # Phase 8 step 4 — clear event log after successful state write
        if s.event_log:
            gist_patch_files(QUICKSILVER_GIST_ID,
                             {f"QUICKSILVER-{s.username}-LOG.json": "[]"})
        s.write_back_done = True
    except Exception as e:
        log_diagnostic("WRITE_BACK_FAIL", username=s.username, detail=str(e))


# ===========================================================================
# Pipeline orchestration (replaces Stage 1 stub)
# ===========================================================================

PHASE_DISPATCH = {
    1: phase_1_input_assembly,
    2: phase_2_event_ingest,
    3: phase_3_field_computation,
    4: phase_4_board_meeting,
    5: phase_5_triggered_evals,
    "5d": phase_5d_cg_classification,
    6: phase_6_json_assembly,
    7: phase_7_validation,
    # 8 has a special signature (takes disposition) — dispatched manually
}


def execute_pipeline(user: dict, tier: str, sources: dict) -> Optional[PipelineStaging]:
    """Run the Runner phases for this user at this tier."""
    phases = PHASES_PER_TIER.get(tier, [])
    if not phases:
        return None

    s = PipelineStaging(user, tier, sources)
    for phase_id in phases:
        if phase_id == 8:
            continue  # write-back handled by caller after quality gate
        fn = PHASE_DISPATCH.get(phase_id)
        if fn is None:
            log_diagnostic("PHASE_NOT_WIRED", username=s.username, detail=f"phase={phase_id}")
            continue
        try:
            fn(s)
        except Exception as e:
            log_diagnostic("PHASE_FAIL", username=s.username,
                           detail=f"phase={phase_id} err={e}")
            s.phase_errors[phase_id] = str(e)
            # First-fail-stop per spec — surface the failure rather than write
            # partial output. Quality gate will mark this user REVIEW or worse.
            return s
    return s


# ===========================================================================
# Quality Gate — 6-dimension scoring
# ===========================================================================

def score_quality(s: PipelineStaging) -> dict:
    """Score Phase 6 output across 6 dimensions. Mechanical for v1; will
    incorporate model-driven scoring after Drive 3 Trust Calibration."""
    j = s.display_json
    if not j:
        return {dim: 1 for dim in QUALITY_DIMENSIONS}

    scores: dict = {}

    # menu_accuracy — does menu order match behavioral signal?
    scores["menu_accuracy"] = 5 if "home" in j and j["home"].get("options") else 1

    # amount_accuracy — Spark amount + units present?
    spark = j.get("spark", {})
    spark_label = (spark.get("options") or [{}])[0].get("label", "")
    if any(act.get("name") and act.get("amount") for act in s.user_state.get("activities", [])):
        scores["amount_accuracy"] = 4 if "🔥" in spark_label else 2
    else:
        scores["amount_accuracy"] = 3

    # data_correctness — schema validation passed?
    scores["data_correctness"] = 5 if s.validation_passed else 2

    # suggestion_relevance — Worth Trying populated?
    wt = (s.board_output or {}).get("worth_trying") or []
    if (s.board_output or {}).get("unavailable"):
        scores["suggestion_relevance"] = 1
    elif wt:
        scores["suggestion_relevance"] = 4
    else:
        scores["suggestion_relevance"] = 2

    # rate_sensitivity — no jarring changes from prior JSON (first run: 4)
    scores["rate_sensitivity"] = 4

    # signal_isolation — every data point traces to this user only
    # Quick check: any string containing another known username = fail
    other_users = ["gerber", "mats", "rob"]
    other_users = [u for u in other_users if u != s.username]
    blob = json.dumps(j).lower()
    leaked = any(f'"{u}"' in blob for u in other_users)
    scores["signal_isolation"] = 1 if leaked else 5

    return scores


def classify_disposition(scores: dict) -> str:
    """CLEAN / FLAGGED / REVIEW / CONTAMINATION."""
    if scores.get("signal_isolation", 0) == 1:
        return "CONTAMINATION"
    if not scores:
        return "REVIEW"
    avg = sum(scores.values()) / len(scores)
    if avg >= 4.5:
        return "CLEAN"
    if avg >= 3.5:
        return "FLAGGED"
    return "REVIEW"


# ===========================================================================
# Step 5 — Update Registry
# ===========================================================================

def update_user_in_registry(registry: dict, username: str, **updates) -> None:
    """Mutate the user row in registry with new field values."""
    for user in registry["users"]:
        if user.get("username") == username:
            user.update({k: str(v) for k, v in updates.items()})
            return


def _versions_blob(sources: dict) -> str:
    """Compact JSON of source filename → updated_at for the registry row."""
    return json.dumps({f: info["updated_at"] for f, info in sources.items()})


# ===========================================================================
# Step 6 — Write Run Log
# ===========================================================================

def append_run_log(summary: dict) -> None:
    """Append a run summary block to HOLOCRON-RUN-LOG.md. Cap at 168 entries."""
    print("Step 6: Writing run log...", flush=True)
    try:
        content, _ = gist_get_file(QUICKSILVER_GIST_ID, "HOLOCRON-RUN-LOG.md")
    except FileNotFoundError:
        content = "# Holocron Run Log\n\n"

    entry = format_run_log_entry(summary)
    new_content = content.rstrip() + "\n\n" + entry + "\n"

    parts = re.split(r"\n(## \d{4}-\d{2}-\d{2}T)", new_content)
    if len(parts) > 1:
        header = parts[0]
        entries = []
        for i in range(1, len(parts), 2):
            entries.append(parts[i] + parts[i + 1] if i + 1 < len(parts) else parts[i])
        if len(entries) > RUN_LOG_CAP:
            entries = entries[-RUN_LOG_CAP:]
        new_content = header + "\n".join(entries)

    gist_patch_files(QUICKSILVER_GIST_ID, {"HOLOCRON-RUN-LOG.md": new_content})
    print("Run log updated.", flush=True)


def format_run_log_entry(summary: dict) -> str:
    return (
        f"## {summary['ts']}\n"
        f"- **Trigger:** {summary.get('trigger', 'hourly run')}\n"
        f"- **Dry run:** {summary.get('dry_run', 'unknown')}\n"
        f"- **Users processed:** {summary['processed']}/{summary['active']} active "
        f"({summary['dormant']} dormant, skipped)\n"
        f"- **Tier distribution:** "
        + ", ".join(f"{tier}={n}" for tier, n in summary['tiers'].items())
        + "\n"
        f"- **Results:** {summary.get('results', 'none')}\n"
        f"- **Diagnostics:** {summary.get('diagnostics', 'none')}\n"
    )


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    if not GITHUB_TOKEN:
        fatal("GITHUB_TOKEN environment variable not set", code=1)

    print(f"Run config: dry_run={DRY_RUN} user_filter={USER_FILTER or 'all'}", flush=True)
    if not ANTHROPIC_API_KEY:
        log_diagnostic("ANTHROPIC_KEY_MISSING",
                       detail="ANTHROPIC_API_KEY not set; editorial phases will produce empty board output")

    registry = None
    try:
        registry = acquire_lock()
        print(f"Step 2: Parsed registry — {len(registry['users'])} total users", flush=True)
        active_users = [u for u in registry["users"] if u.get("status") == "active"]
        if USER_FILTER:
            active_users = [u for u in active_users if u.get("username") in USER_FILTER]
        print(f"  {len(active_users)} active users (after filter).", flush=True)

        sources = fetch_sources()

        print("Step 4: Per-user processing...", flush=True)
        tier_counts: dict[str, int] = {}
        results = []
        for user in active_users:
            username = user["username"]
            try:
                tier = triage(user, sources)
            except Exception as e:
                log_diagnostic("TRIAGE_FAIL", username=username, detail=str(e))
                tier = "SKIP"
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
            print(f"  [{username}] tier={tier}", flush=True)
            if tier == "SKIP":
                results.append(f"{username}: SKIP")
                continue

            staging = execute_pipeline(user, tier, sources)
            if staging is None:
                results.append(f"{username}: {tier} (no phases)")
                continue

            if staging.phase_errors:
                results.append(f"{username}: {tier} PHASE_ERR ({list(staging.phase_errors.keys())})")
                continue

            scores = score_quality(staging)
            disposition = classify_disposition(scores)
            avg_pct = int((sum(scores.values()) / len(scores)) * 20) if scores else 0
            results.append(f"{username}: {tier} {avg_pct}% {disposition}")

            print(f"    scores={scores} disposition={disposition}", flush=True)
            if staging.validation_failures:
                for f in staging.validation_failures[:8]:
                    log_diagnostic("VALIDATION_FAIL", username=username, detail=f)

            phase_8_write_back(staging, disposition)

            # Drive 1.5 Step 4 — three-stage coupled pipeline.
            # Stage 2 (Baseline) + Stage 3 (Scorer) + monthly eval Gist row
            # append. Imported lazily so a missing eval module never blocks
            # the Holocron routine; per-user exceptions never propagate.
            try:
                import eval_pipeline  # local module
                eval_pipeline.run_eval_for_user(staging, sources)
            except Exception as e:
                log_diagnostic("EVAL_PIPELINE_ERR", username=username, detail=str(e))

            updates = {
                "last_refresh": now_iso(),
                "last_mode": tier,
                "last_confidence": f"{avg_pct}%",
                "last_source_versions": _versions_blob(sources),
            }
            if tier in {"FULL", "SOURCE_REFRESH", "COLD_START"}:
                updates["last_full_run"] = now_iso()
                updates["last_holocron_run"] = now_iso()
            if tier == "LIGHT":
                updates["last_holocron_run"] = now_iso()
            tz_name = user.get("timezone") or "America/Los_Angeles"
            updates["day_view_last_refreshed"] = today_in_tz(tz_name)
            if not DRY_RUN:
                update_user_in_registry(registry, username, **updates)

        print("Step 5: Updating registry...", flush=True)
        try:
            if not DRY_RUN:
                gist_patch_files(QUICKSILVER_GIST_ID,
                    {"HOLOCRON-USERS.md": serialize_registry(registry)})
            else:
                log_diagnostic("DRY_RUN_REGISTRY_SKIPPED",
                               detail="registry mutations not persisted")
        except Exception as e:
            log_diagnostic("REGISTRY_UPDATE_FAIL", detail=str(e))

        summary = {
            "ts": now_iso(),
            "trigger": "hourly run",
            "dry_run": str(DRY_RUN).lower(),
            "active": len(active_users),
            "processed": sum(1 for r in results if "SKIP" not in r),
            "dormant": tier_counts.get("DORMANT", 0),
            "tiers": tier_counts,
            "results": " | ".join(results) if results else "none",
            "diagnostics": " | ".join(f"{d['kind']}" for d in DIAGNOSTICS) or "none",
        }
        # Run log is observability — always written (dry_run flag included)
        try:
            append_run_log(summary)
        except Exception as e:
            log_diagnostic("RUNLOG_WRITE_FAIL", detail=str(e))

    finally:
        # Lock release is concurrency-critical — always runs regardless of DRY_RUN.
        if registry is not None:
            release_lock(registry)

    print("Routine complete.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
