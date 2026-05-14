"""
Evaluation Pipeline — Drive 1.5 Step 4
======================================
Three-stage coupled pipeline glue. Run after Holocron's Phase 8 to produce
the Baseline output and the independent Scorer's per-element scores, then
append rows to the monthly evaluation file on the dedicated eval Gist.

Stages (per QB-BASELINE-RUNNER.md §Three-Stage Pipeline Design):

  Stage 1: Holocron pipeline runs → produces display_json + tokens_used
           (handled in holocron_runner.execute_pipeline; the staging is
           passed into this module).
  Stage 2: Baseline runner runs on identical inputs → display_json + tokens
           (baseline_runner.run_baseline_for_user, called here).
  Stage 3: Independent Scorer — single Claude call given only the two
           outputs + the Element Registry + Scoring Model. Returns
           per-element scores for both models. Lift and zone are computed
           deterministically from the scores and token counts.

Output: rows appended to EVAL-YYYY-MM.md on the eval Gist. One row per
(run, user, element). Token columns repeat across the user's element rows
(see BASELINE-SCORING-MODEL.md §Token Counting — tokens cannot be
attributed per element).

Environment variables:
    EVAL_PIPELINE_ENABLED — default "true". Set to "false" to skip Stages 2-3.
    EVAL_DRY_RUN          — default mirrors HOLOCRON_DRY_RUN. When true, the
                            scorer still runs and rows are computed, but the
                            eval Gist PATCH is skipped (rows are staged to
                            /tmp/eval-rows-<user>.json for inspection).
    EVAL_GIST_ID          — override the default eval Gist ID. Empty = use
                            DEFAULT_EVAL_GIST_ID.
    SCORER_MODEL          — override the Scorer model (default
                            claude-sonnet-4-6).
"""

import json
import os
import re
import sys
import urllib.error

from holocron_runner import (
    DRY_RUN as HOLOCRON_DRY_RUN,
    QUICKSILVER_GIST_ID,
    fetch_sources,
    gist_get_file,
    gist_patch_files,
    log_diagnostic,
    now_iso,
)
import baseline_runner

# ===========================================================================
# Configuration
# ===========================================================================

DEFAULT_EVAL_GIST_ID = "5892d49a4d386d09a919ccae13bef709"
EVAL_GIST_ID = os.environ.get("EVAL_GIST_ID", "").strip() or DEFAULT_EVAL_GIST_ID

EVAL_ENABLED = os.environ.get("EVAL_PIPELINE_ENABLED", "true").strip().lower() == "true"
_eval_dry_env = os.environ.get("EVAL_DRY_RUN", "").strip().lower()
EVAL_DRY_RUN = (_eval_dry_env == "true") if _eval_dry_env else HOLOCRON_DRY_RUN

SCORER_MODEL = os.environ.get("SCORER_MODEL", "claude-sonnet-4-6")
SCORER_MAX_TOKENS = 4096

# Element identifiers in the Element Registry (BASELINE-ELEMENT-REGISTRY.md v1.00).
# Order matters: rows are emitted in this order for stable diffs.
ELEMENT_IDS = [
    "D1", "D2", "D3", "D4", "D5", "D6", "D7",
    "P1", "P2", "P3", "P4", "P5",
    "S1", "S2", "S3", "S4", "S5",
]

# Decision zone thresholds per BASELINE-SCORING-MODEL.md §Decision Zones.
WIDE_GAP_LIFT_PCT = 30.0
BASELINE_WIN_LIFT_PCT = -15.0


# ===========================================================================
# Stage 3 — Independent Scorer
# ===========================================================================

SCORER_SYSTEM_PROMPT = """You are the independent Scorer for the
Quicksilver Baseline Runner evaluation pipeline (Drive 1.5).

You receive TWO display JSONs — one produced by the Holocron pipeline, one
produced by the Baseline runner — both for the same user, same inputs. You
also receive the Element Registry (what to score) and the Scoring Model
(how to score). You DO NOT receive either model's prompts or internal
reasoning. You evaluate the outputs only.

Score each applicable element on the 1-5 scale defined in the Element
Registry per category model (P1 for Data, E04 for Prose, N1 for Structure).
If an element does not apply to this user (e.g., D6 Onboarding for a
non-onboarding user, D7 Content Governance when Phase 5d did not run),
return "skip" for both models on that element.

OUTPUT FORMAT — strict JSON, no prose, no markdown fences:

{
  "scores": {
    "D1": {"holocron": 4, "baseline": 3, "notes": "short reason"},
    "D2": {"holocron": "skip", "baseline": "skip", "notes": "n/a"},
    ...
  }
}

One entry per element in the registry (D1-D7, P1-P5, S1-S5). Score values
are integers 1-5 or the literal string "skip". The "notes" field is one
short sentence (≤ 20 words) explaining the score. Be specific and
evidence-based — cite what's in the JSON, not what you think the model
should have done.

Score the two models against the SAME criteria. Do not favor either model.
You have no information about which JSON is "better-built" — only what
each JSON contains."""


def _parse_scorer_response(text: str) -> dict:
    """Strip optional code fences, parse JSON. Returns {} on failure."""
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


def _fetch_eval_specs(sources: dict) -> tuple[str, str]:
    """Return (element_registry_md, scoring_model_md) from the Quicksilver Gist.

    Specs may already be in `sources` if the caller fetched them; otherwise
    fall back to a direct Gist read so the Scorer never runs without its
    grading rubric.
    """
    reg = (sources or {}).get("BASELINE-ELEMENT-REGISTRY.md", {}).get("content")
    scm = (sources or {}).get("BASELINE-SCORING-MODEL.md", {}).get("content")
    if not reg:
        try:
            reg, _ = gist_get_file(QUICKSILVER_GIST_ID, "BASELINE-ELEMENT-REGISTRY.md")
        except Exception as e:
            raise RuntimeError(f"BASELINE-ELEMENT-REGISTRY.md fetch failed: {e}")
    if not scm:
        try:
            scm, _ = gist_get_file(QUICKSILVER_GIST_ID, "BASELINE-SCORING-MODEL.md")
        except Exception as e:
            raise RuntimeError(f"BASELINE-SCORING-MODEL.md fetch failed: {e}")
    return reg, scm


def run_scorer(username: str, holocron_json: dict, baseline_json: dict,
               element_registry_md: str, scoring_model_md: str,
               model: str | None = None) -> dict:
    """Call the Scorer model. Returns dict with `scores` and `usage`."""
    model = model or SCORER_MODEL
    out = {
        "model": model,
        "scores": {},
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "error": None,
    }
    try:
        import anthropic
    except ImportError:
        out["error"] = "anthropic SDK not installed"
        return out

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        out["error"] = "ANTHROPIC_API_KEY not set"
        return out

    client = anthropic.Anthropic(api_key=api_key)
    user_msg = "\n".join([
        f"User: {username}",
        "",
        "== Holocron display JSON ==",
        "```json",
        json.dumps(holocron_json, indent=2, sort_keys=True),
        "```",
        "",
        "== Baseline display JSON ==",
        "```json",
        json.dumps(baseline_json, indent=2, sort_keys=True),
        "```",
        "",
        "== BASELINE-ELEMENT-REGISTRY.md ==",
        element_registry_md,
        "",
        "== BASELINE-SCORING-MODEL.md ==",
        scoring_model_md,
    ])

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=SCORER_MAX_TOKENS,
            system=SCORER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text if resp.content else ""
        parsed = _parse_scorer_response(text)
        scores = parsed.get("scores") if isinstance(parsed, dict) else None
        out["scores"] = scores or {}
        it = getattr(resp.usage, "input_tokens", 0) if resp.usage else 0
        ot = getattr(resp.usage, "output_tokens", 0) if resp.usage else 0
        out["usage"] = {
            "input_tokens": it, "output_tokens": ot, "total_tokens": it + ot,
        }
        out["raw_text"] = text
        if not out["scores"]:
            out["error"] = "Scorer returned no parseable scores"
    except Exception as e:
        log_diagnostic("SCORER_API_ERR", username=username, detail=str(e))
        out["error"] = str(e)
    return out


# ===========================================================================
# Lift + Zone math (deterministic; see BASELINE-SCORING-MODEL.md)
# ===========================================================================

def _element_lift_pct(h: int | str, b: int | str) -> float | None:
    """Per-element lift = (H - B) / B × 100. None if skipped or B is 0."""
    if h == "skip" or b == "skip":
        return None
    try:
        hv = float(h); bv = float(b)
    except (TypeError, ValueError):
        return None
    if bv == 0:
        return None
    return (hv - bv) / bv * 100.0


def _zone_for_lift(lift_pct: float | None) -> str:
    if lift_pct is None:
        return "n/a"
    if lift_pct > WIDE_GAP_LIFT_PCT:
        return "Wide gap"
    if lift_pct < BASELINE_WIN_LIFT_PCT:
        return "Baseline wins"
    return "Narrow gap"


# ===========================================================================
# Stage 4 — Append rows to monthly eval file
# ===========================================================================

ROW_HEADER_RE = re.compile(
    r"^\|\s*Date\s*\(UTC\)\s*\|\s*User\s*\|\s*Element\s*\|.*\|\s*Zone\s*\|\s*$",
    re.MULTILINE,
)


def _monthly_filename(ts_iso: str | None = None) -> str:
    """EVAL-YYYY-MM.md derived from UTC date in ts_iso (default: now)."""
    ts = ts_iso or now_iso()
    # ts_iso shape: 2026-05-14T22:53:00Z — first 7 chars are YYYY-MM
    yyyy_mm = ts[:7]
    return f"EVAL-{yyyy_mm}.md"


def _seed_monthly_template(yyyy_mm: str) -> str:
    """Initial content for a new monthly file. Matches the bootstrapped
    EVAL-2026-05.md structure so all months share the same shape."""
    return (
        f"# Quicksilver Evaluation — {yyyy_mm}\n\n"
        "**Schema:** Per BASELINE-ELEMENT-REGISTRY.md (v1.00) and "
        "BASELINE-SCORING-MODEL.md (v1.00) on the Quicksilver Gist.\n\n"
        "**One row per (run, user, element).** Token columns repeat for "
        "every element of the same (run, user); they describe the *total* "
        "tokens that produced the run's display JSON, not per-element "
        "attribution (see Scoring Model §Token Counting).\n\n"
        "---\n\n"
        "## Run Log\n\n"
        "| Date (UTC) | User | Element | Holocron Score | Holocron Tokens "
        "| Baseline Score | Baseline Tokens | Lift % | Zone |\n"
        "|------------|------|---------|----------------|"
        "-----------------|----------------|"
        "-----------------|--------|------|\n\n"
        "## Summaries\n\n"
        "_Weekly and monthly summaries appended below as runs accumulate. "
        "Template: BASELINE-SCORING-MODEL.md §Summary Templates._\n"
    )


def _format_token_cell(t: int) -> str:
    """Render token count for the table. Use raw integer for accuracy."""
    return str(int(t))


def _format_score_cell(v) -> str:
    return str(v) if v != "skip" else "skip"


def _format_lift_cell(lift_pct: float | None) -> str:
    if lift_pct is None:
        return "—"
    sign = "+" if lift_pct >= 0 else ""
    return f"{sign}{lift_pct:.1f}%"


def build_rows(run_ts: str, username: str, scores: dict,
               holocron_tokens: int, baseline_tokens: int) -> list[str]:
    """Render markdown table rows for one (run, user) across all elements
    present in `scores`. Elements not returned by the Scorer are omitted
    (no phantom rows)."""
    rows = []
    for eid in ELEMENT_IDS:
        entry = scores.get(eid)
        if not isinstance(entry, dict):
            continue
        h = entry.get("holocron")
        b = entry.get("baseline")
        if h is None and b is None:
            continue
        lift = _element_lift_pct(h, b)
        zone = _zone_for_lift(lift)
        row = (
            f"| {run_ts} | {username} | {eid} | "
            f"{_format_score_cell(h)} | {_format_token_cell(holocron_tokens)} | "
            f"{_format_score_cell(b)} | {_format_token_cell(baseline_tokens)} | "
            f"{_format_lift_cell(lift)} | {zone} |"
        )
        rows.append(row)
    return rows


def _insert_rows(existing: str, rows: list[str]) -> str:
    """Insert new rows immediately after the Run Log header separator.

    The header is two adjacent lines: column row + separator row. New rows
    go right after the separator, before any blank line that follows, so
    chronological order is newest-first at the top of the table (easy to
    skim). Falls back to appending at end-of-table if the header isn't
    where we expect.
    """
    lines = existing.splitlines()
    new_lines = []
    inserted = False
    i = 0
    while i < len(lines):
        new_lines.append(lines[i])
        if (not inserted
                and lines[i].startswith("| Date (UTC)")
                and i + 1 < len(lines)
                and lines[i + 1].lstrip().startswith("|-")):
            new_lines.append(lines[i + 1])
            new_lines.extend(rows)
            inserted = True
            i += 2
            continue
        i += 1
    if not inserted:
        new_lines.append("")
        new_lines.extend(rows)
    return "\n".join(new_lines) + ("\n" if existing.endswith("\n") else "")


def append_eval_rows(filename: str, rows: list[str]) -> None:
    """Read EVAL-YYYY-MM.md from the eval Gist, splice rows, PATCH back.

    Creates the file from a template if missing. EVAL_DRY_RUN guards the
    network write (rows still computed and surfaced).
    """
    try:
        content, _ = gist_get_file(EVAL_GIST_ID, filename)
    except FileNotFoundError:
        content = _seed_monthly_template(filename[5:-3])  # EVAL-YYYY-MM.md → YYYY-MM
    except urllib.error.HTTPError as e:
        log_diagnostic("EVAL_FILE_FETCH_FAIL", detail=f"{filename}: {e}")
        content = _seed_monthly_template(filename[5:-3])

    updated = _insert_rows(content, rows)
    if EVAL_DRY_RUN:
        log_diagnostic("EVAL_DRY_RUN_SKIPPED",
                       detail=f"{filename}: {len(rows)} rows staged (not written)")
        return
    gist_patch_files(EVAL_GIST_ID, {filename: updated})


# ===========================================================================
# Orchestration entry point — called from holocron_runner.main()
# ===========================================================================

def run_eval_for_user(staging, sources: dict) -> dict | None:
    """Execute Stages 2 + 3 + write rows for one user. Returns the
    result record (dict) or None when disabled / skipped.

    Per CLAUDE.md "Per-user isolation": exceptions never propagate to the
    caller. All failures log a diagnostic and return the result dict with
    the relevant `error` field populated.
    """
    username = staging.username
    if not EVAL_ENABLED:
        log_diagnostic("EVAL_SKIPPED", username=username, detail="EVAL_PIPELINE_ENABLED=false")
        return None

    if not staging.display_json:
        log_diagnostic("EVAL_SKIPPED", username=username,
                       detail="no holocron display_json — skipping eval")
        return None

    run_ts = now_iso()

    # Stage 2 — Baseline runner
    baseline_result = baseline_runner.run_baseline_for_user(
        username, staging.user_state, staging.event_log, sources,
    )
    if baseline_result.get("error"):
        log_diagnostic("EVAL_BASELINE_FAIL", username=username,
                       detail=baseline_result["error"])
        # Continue to scorer only if a parseable display_json came back.
        if not baseline_result.get("display_json"):
            return {"username": username, "phase": "baseline",
                    "error": baseline_result["error"]}

    # Stage 3 — Scorer
    try:
        reg_md, scm_md = _fetch_eval_specs(sources)
    except Exception as e:
        log_diagnostic("EVAL_SPECS_FAIL", username=username, detail=str(e))
        return {"username": username, "phase": "specs", "error": str(e)}

    scorer = run_scorer(
        username, staging.display_json, baseline_result.get("display_json") or {},
        reg_md, scm_md,
    )
    if scorer.get("error"):
        log_diagnostic("EVAL_SCORER_FAIL", username=username, detail=scorer["error"])
        return {"username": username, "phase": "scorer", "error": scorer["error"],
                "baseline": baseline_result, "scorer": scorer}

    holocron_tokens = staging.tokens_used.get("total_tokens", 0)
    baseline_tokens = (baseline_result.get("usage") or {}).get("total_tokens", 0)

    rows = build_rows(run_ts, username, scorer["scores"],
                      holocron_tokens, baseline_tokens)
    filename = _monthly_filename(run_ts)

    # Always stage to /tmp for inspection regardless of dry-run mode.
    stage_path = f"/tmp/eval-rows-{username}.json"
    try:
        with open(stage_path, "w") as f:
            json.dump({
                "filename": filename,
                "rows": rows,
                "scores": scorer["scores"],
                "holocron_tokens": holocron_tokens,
                "baseline_tokens": baseline_tokens,
                "baseline_usage": baseline_result.get("usage"),
                "scorer_usage": scorer.get("usage"),
            }, f, indent=2)
    except Exception as e:
        log_diagnostic("EVAL_STAGE_WRITE_FAIL", username=username, detail=str(e))

    try:
        append_eval_rows(filename, rows)
    except Exception as e:
        log_diagnostic("EVAL_APPEND_FAIL", username=username,
                       detail=f"{filename}: {e}")
        return {"username": username, "phase": "append", "error": str(e),
                "filename": filename, "rows": rows, "scorer": scorer,
                "baseline": baseline_result}

    return {
        "username": username,
        "filename": filename,
        "rows": rows,
        "holocron_tokens": holocron_tokens,
        "baseline_tokens": baseline_tokens,
        "scorer_usage": scorer.get("usage"),
        "baseline_usage": baseline_result.get("usage"),
        "scores": scorer["scores"],
    }


# ===========================================================================
# CLI — manual end-to-end test on one user (independent of the routine)
# ===========================================================================

def _cli_one_user(username: str) -> int:
    """Manual harness: run Holocron + eval pipeline for one user, end-to-end.

    Use for Step 4 validation. Mirrors what the routine does but without
    the registry update / lock / run log work.
    """
    from holocron_runner import (
        triage, execute_pipeline, score_quality, classify_disposition,
        phase_8_write_back,
    )

    sources = fetch_sources()
    user_row = {"username": username, "status": "active"}
    tier = triage(user_row, sources)
    print(f"[{username}] tier={tier}", flush=True)
    if tier == "SKIP":
        print("triage SKIP — nothing to do", flush=True)
        return 0

    staging = execute_pipeline(user_row, tier, sources)
    if staging is None:
        print("no phases executed", flush=True)
        return 1
    if staging.phase_errors:
        print(f"phase errors: {staging.phase_errors}", flush=True)

    scores = score_quality(staging)
    disposition = classify_disposition(scores)
    print(f"quality scores={scores} disposition={disposition}", flush=True)

    phase_8_write_back(staging, disposition)

    result = run_eval_for_user(staging, sources)
    print(f"eval result keys: {list((result or {}).keys())}", flush=True)
    if result and "rows" in result:
        print(f"eval wrote {len(result['rows'])} rows to {result['filename']} "
              f"(dry_run={EVAL_DRY_RUN})", flush=True)
        for r in result["rows"][:5]:
            print(f"  {r}", flush=True)
    return 0


if __name__ == "__main__":
    user = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("EVAL_USER", "mats")
    sys.exit(_cli_one_user(user))
