"""
Baseline Runner — Drive 1.5
===========================
Produces the Quicksilver display JSON for a user using a SINGLE Claude API
call with a simpler prompt. Companion to ``holocron_runner.py``.

Holocron = "do whatever it takes" — 8 phases, deterministic computation
plus editorial calls. Baseline = "most direct, sensible, easiest, stable
way" — one call, one prompt, one JSON.

The baseline is the independent variable in the benefit-per-token
comparison defined in QB-BASELINE-RUNNER.md / BASELINE-ELEMENT-REGISTRY.md
/ BASELINE-SCORING-MODEL.md. **Flaw-only maintenance.** Fix correctness
bugs (missing elements, parse failures, crashes); never optimize quality.
The comparison loses meaning if the baseline moves.

The Step 3 deliverable (this script) produces a baseline display JSON for
one or more users and prints/stages the result. Step 4 wires the Stage-2
invocation into the Holocron pipeline and the eval Gist write-back.

Inputs (identical to Holocron's factual inputs):
    - USER-<user>.json on the NW State Gist (user state)
    - QUICKSILVER-<user>-LOG.json on the Quicksilver Gist (event log)
    - QUICKSILVER-SCHEMA.md  (output shape)
    - QUICKSILVER-CONTENT.md (shared content authority)
    - NW-MENUS.md            (menu definitions)
    - QUICKSILVER-CONSTITUTION.md (optional, included if present)

Intentional omission: HOLOCRON-RUNNER.md (the per-phase execution
playbook). Handing Holocron's playbook to the baseline would defeat the
purpose of testing whether per-phase complexity pays off. Flagged in the
QB doc; Bridge to adjust if they disagree.

Environment variables:
    GITHUB_TOKEN          — required for fetching inputs from Gist
    ANTHROPIC_API_KEY     — required for the baseline call
    BASELINE_DRY_RUN      — default "true". When true, prints + stages
                            output locally; when false (Step 4), the
                            eval Gist write hook activates.
    BASELINE_USER_FILTER  — comma-separated usernames. Empty = all active.
    BASELINE_MODEL        — optional override (default claude-sonnet-4-6).

Usage:
    # CLI smoke test (single user):
    GITHUB_TOKEN=... ANTHROPIC_API_KEY=... BASELINE_USER_FILTER=mats \\
        python3 baseline_runner.py

    # Module (called by holocron_runner.py in Step 4 pipeline):
    from baseline_runner import run_baseline_for_user
    result = run_baseline_for_user(username, user_state, event_log, sources)
"""

import json
import os
import re
import sys
import urllib.error

from holocron_runner import (
    QUICKSILVER_GIST_ID,
    NW_STATE_GIST_ID,
    fetch_sources,
    gist_get_file,
    log_diagnostic,
    now_iso,
    parse_registry,
)

# ===========================================================================
# Configuration
# ===========================================================================

BASELINE_MODEL = os.environ.get("BASELINE_MODEL", "claude-sonnet-4-6")
BASELINE_DRY_RUN = os.environ.get("BASELINE_DRY_RUN", "true").strip().lower() == "true"
BASELINE_USER_FILTER = [
    u.strip()
    for u in os.environ.get("BASELINE_USER_FILTER", "").split(",")
    if u.strip()
]
BASELINE_MAX_TOKENS = 8192

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# Source files we forward to the baseline call. Order matters only for
# readability of the prompt — the model can re-order as needed.
BASELINE_SOURCE_FILES = [
    "QUICKSILVER-SCHEMA.md",
    "QUICKSILVER-CONTENT.md",
    "NW-MENUS.md",
    "QUICKSILVER-CONSTITUTION.md",  # optional; included when present
]


# ===========================================================================
# Prompt
# ===========================================================================

SYSTEM_PROMPT = """You are the Quicksilver display generator.

Read the user's state, event log, and source specs (schema, content, menus,
optional constitution), then produce the COMPLETE Quicksilver display JSON
for this user in ONE shot. No multi-step reasoning visible to the user.

REQUIRED TOP-LEVEL KEYS:
  home, today, spark, spark_done, more, explore,
  progress, progress_spark, progress_groups, progress_signals,
  progress_worth_trying,
  together, together_connections, together_messaging.

PATTERN KEYS:
  - One `more_<slug>_done` per markable More activity. Slug = lowercase name
    with non-alphanumerics collapsed to underscores.
  - One `together_msg_<slug>` per connection if connections exist.
  - Stack keys (`stack_<slug>`) if the user has stacks.
  - `onboarding` ONLY if the user is in onboarding state.

SHAPE OF A KEY:
  - Menu-mode:   {"question": "...", "options": [{"label": "...", "goto": "..."}, ...]}
  - Report-mode: {"inline": "...", "question": "...", "options": [...]}
                  (used on progress landing and its sub-keys)
  - Hybrid:      include both `inline` and `options`.
  - `explore`:   {"session_limit": 1, "queue": [{"inline": "...",
                                                  "affirmation": "..."}, ...]}
                  Queue depth ≥ 2.

NAVIGATION INTEGRITY:
  - Every `goto` must reference an existing key in this JSON.
  - Every non-home screen has a back option using the `❮` prefix as the
    LAST option, pointing to its parent per the breadcrumb hierarchy.
  - Question text uses breadcrumbs with the `›` separator.

CONTENT RULES (per QUICKSILVER-CONTENT.md):
  - Spark question word: "Done?" for single activities, "Ready?" for stacks.
  - Worth Trying: title ≤ 8 words, body ≤ 25 words, with a one-sentence
    "why now" grounded in actual signal (no generic advice).
  - Celebration text: ≤ 15 words, warm, specific, identity-reinforcing.
  - Affirmations: from the QUICKSILVER-CONTENT.md §Affirmations set, no
    repeats inside one explore queue.
  - Explore questions: verbatim from QUICKSILVER-CONTENT.md banks
    where possible; no recent repeats from question_history.

GROUNDING:
  - Every claim about the user must trace to user_state. No fabricated
    achievements, no invented streaks, no phantom connections.
  - For users with less than 2 weeks of data, prefer encouragement
    language over pattern claims.

OUTPUT:
  Respond with ONLY the JSON object. No markdown code fences. No prose
  before or after. Pure JSON, starting with `{` and ending with `}`.
"""


def build_user_message(username: str, user_state: dict, event_log: list,
                       sources: dict) -> str:
    """Assemble the user-message payload sent alongside the system prompt."""
    parts = [
        f"User: {username}",
        "",
        "== User State ==",
        "```json",
        json.dumps(user_state, indent=2, sort_keys=True),
        "```",
        "",
        "== Event Log ==",
        "```json",
        json.dumps(event_log, indent=2),
        "```",
        "",
    ]
    for src_name in BASELINE_SOURCE_FILES:
        src = sources.get(src_name)
        if not src:
            continue
        parts.append(f"== {src_name} ==")
        parts.append(src["content"])
        parts.append("")
    return "\n".join(parts)


# ===========================================================================
# JSON parsing
# ===========================================================================

def _parse_json_response(text: str) -> dict:
    """Strip optional code fences and parse JSON. Returns {} on failure.

    Mirrors holocron_runner._parse_json_response — duplicated locally so
    the baseline does not silently inherit changes to Holocron's parser.
    """
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
# Core entry point (used by CLI + Step 4 pipeline)
# ===========================================================================

def run_baseline_for_user(username: str, user_state: dict, event_log: list,
                          sources: dict, model: str | None = None) -> dict:
    """Execute one baseline run for one user. Returns a result dict.

    The shape of the returned dict is the stable contract Step 4's Scorer
    reads from. Keep it stable across versions.
    """
    model = model or BASELINE_MODEL
    started = now_iso()
    base = {
        "username": username,
        "model": model,
        "started_at": started,
        "display_json": {},
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "error": None,
    }

    if not ANTHROPIC_API_KEY:
        base["ended_at"] = now_iso()
        base["error"] = "ANTHROPIC_API_KEY not set"
        return base

    try:
        import anthropic
    except ImportError:
        base["ended_at"] = now_iso()
        base["error"] = "anthropic SDK not installed"
        return base

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_msg = build_user_message(username, user_state, event_log, sources)

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=BASELINE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text if resp.content else ""
        display_json = _parse_json_response(text)
        input_tokens = getattr(resp.usage, "input_tokens", 0) if resp.usage else 0
        output_tokens = getattr(resp.usage, "output_tokens", 0) if resp.usage else 0
        base["display_json"] = display_json
        base["raw_text"] = text
        base["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        if not display_json:
            base["error"] = "JSON parse failure on baseline response"
    except Exception as e:
        log_diagnostic("BASELINE_API_ERR", username=username, detail=str(e))
        base["error"] = str(e)
    base["ended_at"] = now_iso()
    return base


# ===========================================================================
# CLI helpers
# ===========================================================================

def fetch_user_inputs(username: str) -> tuple[dict, list]:
    """Fetch the same per-user inputs Holocron Phase 1 fetches.

    Returns (user_state, event_log). Missing files → empty state/log
    (consistent with Phase 1 cold-start handling).
    """
    try:
        content, _ = gist_get_file(NW_STATE_GIST_ID, f"USER-{username}.json")
        user_state = json.loads(content) if content else {}
    except (FileNotFoundError, urllib.error.HTTPError):
        user_state = {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"USER-{username}.json parse failure: {e}")

    try:
        log_content, _ = gist_get_file(
            QUICKSILVER_GIST_ID, f"QUICKSILVER-{username}-LOG.json"
        )
        event_log = json.loads(log_content) if log_content else []
    except (FileNotFoundError, urllib.error.HTTPError):
        event_log = []
    except json.JSONDecodeError:
        event_log = []
    return user_state, event_log


def _stage_result_locally(result: dict) -> str:
    """Write the run record to /tmp for inspection during dry-run."""
    path = f"/tmp/baseline-{result['username']}.json"
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    return path


# ===========================================================================
# Main
# ===========================================================================

def main() -> int:
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        return 1
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    print(
        f"Baseline config: model={BASELINE_MODEL} dry_run={BASELINE_DRY_RUN} "
        f"user_filter={BASELINE_USER_FILTER or 'all'}",
        flush=True,
    )

    print("Step A: Fetching registry...", flush=True)
    content, _ = gist_get_file(QUICKSILVER_GIST_ID, "HOLOCRON-USERS.md")
    registry = parse_registry(content)
    active = [u for u in registry["users"] if u.get("status") == "active"]
    if BASELINE_USER_FILTER:
        active = [u for u in active if u.get("username") in BASELINE_USER_FILTER]
    print(f"  {len(active)} active users (after filter)", flush=True)

    print("Step B: Fetching source files...", flush=True)
    sources = fetch_sources()

    print("Step C: Per-user baseline runs...", flush=True)
    for user in active:
        username = user["username"]
        print(f"\n[{username}] running baseline ({BASELINE_MODEL})...", flush=True)
        try:
            user_state, event_log = fetch_user_inputs(username)
        except Exception as e:
            log_diagnostic("BASELINE_INPUT_FETCH_FAIL",
                           username=username, detail=str(e))
            print(f"  fetch_user_inputs failed: {e}", flush=True)
            continue

        result = run_baseline_for_user(username, user_state, event_log, sources)
        usage = result["usage"]
        keys = len(result.get("display_json") or {})
        err = result.get("error")
        print(
            f"  input_tokens={usage['input_tokens']} "
            f"output_tokens={usage['output_tokens']} "
            f"total={usage['total_tokens']} "
            f"keys={keys} "
            f"error={err or 'none'}",
            flush=True,
        )

        if BASELINE_DRY_RUN:
            path = _stage_result_locally(result)
            print(f"  staged at {path}", flush=True)
        else:
            # Step 4 wires the evaluation Gist write here. Until then,
            # a non-dry-run still produces output but does not persist.
            log_diagnostic(
                "BASELINE_EVAL_WRITE_NOT_WIRED",
                username=username,
                detail="evaluation Gist write is a Step 4 deliverable",
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
