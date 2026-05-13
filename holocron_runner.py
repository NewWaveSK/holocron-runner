"""
Holocron Hourly Runner
======================
Autonomous routine that maintains personalized Quicksilver display data
for active users. Runs hourly via Claude Code Routines (cloud).

This script handles all deterministic logic (locks, fetches, parses, triage,
write-back, registry, run log) in pure Python. Editorial work that requires
model judgment (Worth Trying suggestions, message tone, menu rationale) is
delegated to the Anthropic API.

Environment variables required:
    GITHUB_TOKEN       — token with gist scope (NewWaveSK)
    ANTHROPIC_API_KEY  — Anthropic API key for editorial phases

Exit codes:
    0  — completed successfully
    1  — fatal error before lock acquire (or stale lock cleared but next run owns it)
    2  — registry parse failure
    3  — source fetch failure
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

# ===========================================================================
# Configuration
# ===========================================================================

QUICKSILVER_GIST_ID = "287eb4fd487bff8f06e53bcf6cd18f2b"
NW_STATE_GIST_ID = "961083278c6a59b863314e56c5a60402"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

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

# Diagnostic accumulator — appended to over the run, written to log at end
DIAGNOSTICS: list[dict] = []


# ===========================================================================
# Utility
# ===========================================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_diagnostic(kind: str, username: Optional[str] = None, detail: str = "") -> None:
    """Accumulate a diagnostic event for the run log."""
    DIAGNOSTICS.append({
        "kind": kind,
        "username": username,
        "detail": detail,
        "ts": now_iso(),
    })
    # Also print for live observability in the routine session output.
    user_str = f"[{username}] " if username else ""
    print(f"DIAG {kind}: {user_str}{detail}", flush=True)


def fatal(msg: str, code: int = 1) -> None:
    """Print error and exit with code. Caller should release lock first if held."""
    print(f"FATAL: {msg}", flush=True)
    sys.exit(code)


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
    # Use the gist's updated_at as a coarse version stamp.
    updated_at = data.get("updated_at", "")
    return content, updated_at


def gist_patch_files(gist_id: str, files: dict[str, str]) -> dict:
    """
    PATCH one or more files to a Gist. `files` is {filename: content}.
    Returns the response JSON.
    """
    payload = {
        "files": {fname: {"content": content} for fname, content in files.items()}
    }
    body = json.dumps(payload).encode("utf-8")

    # Pre-PATCH safety scan: refuse to send GitHub tokens in payload.
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
        try:
            acquired = datetime.fromisoformat(acquired_str.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - acquired
            if age < timedelta(hours=LOCK_STALE_HOURS):
                print(f"Another run is active (acquired {age} ago). Exiting.", flush=True)
                sys.exit(1)
            else:
                log_diagnostic("STALE_LOCK_CLEARED", detail=f"acquired_at={acquired_str}, age={age}")
        except (ValueError, TypeError):
            log_diagnostic("STALE_LOCK_CLEARED", detail=f"unparseable acquired_at: {acquired_str!r}")

    # Acquire
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
        # Refetch latest content to avoid clobbering updates from Step 5/6.
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

    Expected format (loose — refine when we see the actual file):
        ## Metadata
        - routine_active: false
        - acquired_at:
        - last_source_versions: {...}

        ## Users
        | username | status | last_session | last_holocron_run | ... |
        ...
    """
    metadata = {}
    users = []

    # Parse metadata: bullet lines `- key: value` under ## Metadata
    in_meta = False
    in_users = False
    lines = content.splitlines()
    user_header_seen = False
    for line in lines:
        if line.startswith("## Metadata"):
            in_meta = True
            in_users = False
            continue
        if line.startswith("## Users"):
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
            # Table row. Skip header and separator lines.
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not user_header_seen:
                user_header_seen = True
                user_columns = cells
                continue
            if all(c.replace("-", "").replace(":", "").strip() == "" for c in cells):
                continue  # separator
            row = dict(zip(user_columns, cells))
            if row.get("username"):
                users.append(row)

    return {
        "metadata": metadata,
        "users": users,
        "user_columns": user_columns if 'user_columns' in locals() else [],
        "raw": content,
    }


def serialize_registry(registry: dict) -> str:
    """
    Write registry back to markdown. Round-trip-safe with parse_registry.
    """
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

    for fname in CRITICAL_SOURCES:
        if fname not in files:
            log_diagnostic("FETCH_FAIL", detail=f"critical source missing: {fname}")
            fatal(f"Critical source missing: {fname}", code=3)
        sources[fname] = {
            "content": files[fname].get("content", ""),
            "updated_at": files[fname].get("raw_url", "").split("/")[-2] if files[fname].get("raw_url") else "",
        }

    for fname in WARN_SOURCES:
        if fname in files:
            sources[fname] = {
                "content": files[fname].get("content", ""),
                "updated_at": files[fname].get("raw_url", "").split("/")[-2] if files[fname].get("raw_url") else "",
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
    """
    Seven-step decision tree. First match wins. Returns tier string.
    """
    username = user["username"]

    # Step 0: USER-{username}.json exists on NW State Gist?
    try:
        gist_get_file(NW_STATE_GIST_ID, f"USER-{username}.json")
        user_json_exists = True
    except (FileNotFoundError, urllib.error.HTTPError):
        user_json_exists = False

    if not user_json_exists:
        return "COLD_START"

    # Step 1: last_session within 60 min?
    last_session = user.get("last_session", "")
    if last_session:
        try:
            ls = datetime.fromisoformat(last_session.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - ls < timedelta(minutes=MID_SESSION_THRESHOLD_MIN):
                return "SKIP"
        except ValueError:
            pass

    # Step 2: dormant (no events 30+ days)?
    # We approximate "no events" as last_session > 30 days ago.
    # When event log inspection is wired, use that instead.
    if last_session:
        try:
            ls = datetime.fromisoformat(last_session.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - ls > timedelta(days=DORMANT_THRESHOLD_DAYS):
                if not user.get("dormant_since"):
                    user["dormant_since"] = now_iso()
                    log_diagnostic("DORMANT_SKIP", username=username,
                                   detail=f"days_since_last_session={(datetime.now(timezone.utc) - ls).days}")
                return "SKIP"
        except ValueError:
            pass

    # Dormant re-entry: dormant_since set AND last_session > dormant_since
    dormant_since_str = user.get("dormant_since", "")
    if dormant_since_str and last_session:
        try:
            ds = datetime.fromisoformat(dormant_since_str.replace("Z", "+00:00"))
            ls = datetime.fromisoformat(last_session.replace("Z", "+00:00"))
            if ls > ds:
                user["dormant_since"] = ""
                user["welcome_back"] = "true"
                return "COLD_START"
        except ValueError:
            pass

    # Step 3: source files changed since user's last_source_versions?
    if sources_changed_for_user(sources, user):
        return "SOURCE_REFRESH"

    # Step 4: explore answers in event log?
    # TODO: fetch QUICKSILVER-{USERNAME}-LOG.json and check for explore_answer events
    has_explore_answers = False  # placeholder
    if has_explore_answers:
        return "FULL"

    # Step 5: last_full_run older than 7 days?
    last_full_run_str = user.get("last_full_run", "")
    if last_full_run_str:
        try:
            lfr = datetime.fromisoformat(last_full_run_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - lfr > timedelta(days=WEEKLY_ANCHOR_DAYS):
                return "FULL"
        except ValueError:
            pass
    else:
        # No record of a full run — treat as anchor due
        return "FULL"

    # Step 6: mark-offs in event log OR midnight in user TZ passed?
    # TODO: event log inspection + timezone-aware day rollover check
    has_markoffs = False  # placeholder
    day_ticked = False  # placeholder
    if has_markoffs or day_ticked:
        return "LIGHT"

    # Step 7: no trigger
    return "SKIP"


# ===========================================================================
# Step 4b — Execute Pipeline (Runner translation — STUB)
# ===========================================================================

def execute_pipeline(user: dict, tier: str, sources: dict) -> Optional[dict]:
    """
    Run the Runner phases for this user at this tier.

    NOT YET IMPLEMENTED. This is the next session's work — translate each
    HOLOCRON-RUNNER.md phase into Python + Anthropic API calls.

    For now, returns None (which the caller treats as SKIP with diagnostic).
    """
    username = user["username"]
    phases = PHASES_PER_TIER.get(tier, [])
    if not phases:
        return None

    log_diagnostic("PIPELINE_STUB", username=username,
                   detail=f"tier={tier} would_run_phases={phases}")
    print(f"  [{username}] tier={tier} — pipeline execution NOT YET IMPLEMENTED", flush=True)
    return None


# ===========================================================================
# Step 4c — Quality Gate (STUB — depends on pipeline output)
# ===========================================================================

def score_quality(user_output: dict) -> dict:
    """
    Score the assembled output on 6 dimensions. Returns {dim: 1-5, ...}.
    Mechanical for now; editorial dimensions move to Claude API in next session.

    NOT YET IMPLEMENTED.
    """
    return {dim: 0 for dim in QUALITY_DIMENSIONS}


def classify_disposition(scores: dict) -> str:
    """
    CLEAN / FLAGGED / REVIEW / CONTAMINATION.
    Signal Isolation = 1 → CONTAMINATION (hard gate).
    """
    if scores.get("signal_isolation", 0) == 1:
        return "CONTAMINATION"
    avg = sum(scores.values()) / max(len(scores), 1)
    if avg >= 4.5:
        return "CLEAN"
    if avg >= 3.5:
        return "FLAGGED"
    return "REVIEW"


# ===========================================================================
# Step 4d — Write Back (STUB)
# ===========================================================================

def write_back(user: dict, user_state: dict, display_json: dict) -> bool:
    """
    PATCH USER-{username}.json to NW State Gist and
    QUICKSILVER-{USERNAME}.json to Quicksilver Gist.

    NOT YET IMPLEMENTED — depends on pipeline output structure.
    """
    return False


# ===========================================================================
# Step 5 — Update Registry
# ===========================================================================

def update_user_in_registry(registry: dict, username: str, **updates) -> None:
    """Mutate the user row in registry with new field values."""
    for user in registry["users"]:
        if user.get("username") == username:
            user.update({k: str(v) for k, v in updates.items()})
            return


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

    # Cap at 168 entries — split on `## ` headers, keep the most recent.
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

    registry = None
    try:
        # Step 1 — Acquire Lock
        registry = acquire_lock()

        # Step 2 — Parse Registry (already done in acquire_lock, just filter)
        print(f"Step 2: Parsed registry — {len(registry['users'])} total users", flush=True)
        active_users = [u for u in registry["users"] if u.get("status") == "active"]
        print(f"  {len(active_users)} active users.", flush=True)

        # Step 3 — Fetch Source Files
        sources = fetch_sources()

        # Step 4 — Per-User Loop
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

            # Execute pipeline (currently a stub)
            pipeline_output = execute_pipeline(user, tier, sources)
            if pipeline_output is None:
                results.append(f"{username}: {tier} stub")
                continue

            # Quality gate
            scores = score_quality(pipeline_output)
            disposition = classify_disposition(scores)
            avg_pct = int((sum(scores.values()) / len(scores)) * 20) if scores else 0
            results.append(f"{username}: {tier} {avg_pct}% {disposition}")

            if disposition == "CONTAMINATION":
                log_diagnostic("CONTAMINATION_HALT", username=username,
                               detail=f"scores={scores}")
                continue

            # Write back
            write_back(user, pipeline_output.get("user_state", {}),
                       pipeline_output.get("display_json", {}))

            # Update registry row
            update_user_in_registry(registry, username,
                last_refresh=now_iso(),
                last_mode=tier,
                last_confidence=avg_pct,
            )

        # Step 5 — Update Registry (write back the mutations)
        print("Step 5: Updating registry...", flush=True)
        try:
            gist_patch_files(QUICKSILVER_GIST_ID,
                {"HOLOCRON-USERS.md": serialize_registry(registry)})
        except Exception as e:
            log_diagnostic("REGISTRY_UPDATE_FAIL", detail=str(e))

        # Step 6 — Write Run Log
        summary = {
            "ts": now_iso(),
            "trigger": "hourly run",
            "active": len(active_users),
            "processed": sum(1 for r in results if "SKIP" not in r),
            "dormant": tier_counts.get("DORMANT", 0),
            "tiers": tier_counts,
            "results": " | ".join(results) if results else "none",
            "diagnostics": " | ".join(f"{d['kind']}" for d in DIAGNOSTICS) or "none",
        }
        append_run_log(summary)

    finally:
        # Step 7 — Release Lock (ALWAYS runs)
        if registry is not None:
            release_lock(registry)

    # Step 8 — Exit
    print("Routine complete.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
