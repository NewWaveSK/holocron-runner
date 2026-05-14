# CLAUDE.md — Holocron Runner

## What This Is

Hourly autonomous routine that maintains personalized Quicksilver display data for all active users. Runs via Claude Code Routines (cloud). The Python script (`holocron_runner.py`) handles all deterministic orchestration. Editorial phases (4-5) call the Anthropic API for content generation.

## Security — Non-Negotiable

- **Never write tokens into any file that gets committed or pushed.** Tokens come from environment variables only (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`).
- **Pre-commit scan:** Before any commit, verify no string matching `ghp_` followed by 20+ alphanumeric characters appears in any staged file. GitHub automatically revokes leaked tokens.
- **No fabricated data.** Every credential, ID, URL, and filename must trace to a named source (environment variable, API response, spec file). If a value is unknown, halt and report the gap. Never infer or guess.

## Environment Setup — Universal

Every Claude Code session in this repo follows this setup. Per-drive context (Gist IDs to fetch, step number, design decisions from Bridge) lives in the handoff prompt. Environment lives here.

### Required Environment Variables

Verify both before any work that needs them. If either is missing, halt and ask the user to export it. Never hardcode token values in this file, in `holocron_runner.py`, or in any committed artifact.

- **`GITHUB_TOKEN`** — Gist authentication (read + write). Required for any Gist GET, any Gist PATCH, any QB doc or registry update. Without it the anonymous GitHub API rate limit (~60 requests/hour) trips after ~7 fetches and writes fail entirely.
  - Verify: `test -n "$GITHUB_TOKEN" && echo "GITHUB_TOKEN: set" || echo "GITHUB_TOKEN: MISSING"`
  - Required scopes: `gist` (read + write).
- **`ANTHROPIC_API_KEY`** — Claude API authentication. Required for editorial phases in `holocron_runner.py` (Phase 4 board meeting, Phase 5 triggered evaluations) and for the entire `baseline_runner.py` call.
  - Verify: `test -n "$ANTHROPIC_API_KEY" && echo "ANTHROPIC_API_KEY: set" || echo "ANTHROPIC_API_KEY: MISSING"`
  - Required: a key with permission to call the configured models (`claude-sonnet-4-6` per `MODEL_CONFIG`; baseline can override via `BASELINE_MODEL`).

If a step requires one of these and it isn't set, **halt immediately and surface the gap to the user**. Do not work around it with anonymous requests or skip the step silently — that's how Baseline Runner Drive 1.5 Session 3 hit the rate limit and left the script unvalidated.

### Git Workflow

- **Branch naming.** All Claude Code work happens on a feature branch named `claude/<descriptive-slug>-<random-suffix>`. The handoff prompt either names the branch or expects you to create one. Never push directly to `main`.
- **Commits.** Use the standard commit footer (`https://claude.ai/code/session_<id>`). Pre-commit secret scan applies — see Security above.
- **PR creation.** When a step's deliverable is "code merged to main," create a PR via the GitHub MCP tools (`mcp__github__create_pull_request`) at the end of the step. Title: short imperative summary. Body: Summary + Test plan sections per the standard template.
- **Merge.** Squash-merge to `main` once review is clean. Use `mcp__github__merge_pull_request` with `merge_method: "squash"`. Do not enable auto-merge unless asked.
- **Review.** For Claude Code-authored branches, a self-review against the relevant spec (BASELINE-ELEMENT-REGISTRY.md, HOLOCRON-RUNNER.md, QUICKSILVER-SCHEMA.md, etc.) is sufficient when no human reviewer is on the drive. Document the review checklist outcome in the PR body or in the session report.

### Session Reporting

At the end of every Claude Code session that's part of a multi-tool drive, update **both**:

1. **The QB doc's Claude Code Context section** (template below). PATCH it on whichever Gist hosts the QB doc.
2. **QB-REGISTRY.md** on the Bridge Gist (`7f983152470de13b03ed60bc0556957b`) — check-in entry with session summary.

```
## Claude Code Context

**Repo:** [org/repo-name]
**Branch:** [current branch]
**Summary:** [1-2 sentences: what was accomplished this session]
**Last Action:** [what was done, when]
**Test Status:** [passing/failing/not-yet-run]
**Open Items:** [anything the next session needs to know]
```

No separate forensic-style report. The Claude Code Context section is the session report. Keep it lightweight; Bridge reads it to validate code work and pick up the drive.

## Gist Operations

All Quicksilver data lives on GitHub Gists, not in this repo. The script reads and writes Gists via the GitHub API using `GITHUB_TOKEN`.

**Key Gist:** Quicksilver Gist `287eb4fd487bff8f06e53bcf6cd18f2b` — hosts user registry, run log, source files (schema, content, menus, runner, explore sets), and per-user display JSONs.

**NW State Gist:** `961083278c6a59b863314e56c5a60402` — hosts per-user state files (USER-gerber.json, USER-mats.json) and event logs.

**Evaluation Gist (Drive 1.5 Step 4):** `5892d49a4d386d09a919ccae13bef709` — Baseline Runner monthly comparison rows. One file per month (`EVAL-YYYY-MM.md`). Written by `eval_pipeline.py` after each user's Holocron run. Owned by the NewWaveSK account.

**Pattern:** Fetch full Gist in one API call → extract files by name → process → PATCH only changed files back. Never inline large payloads in curl `-d` strings. Write to temp file and use `-d @path`.

## Source of Truth — Specs on the Gist

These files define WHAT to build. They live on the Quicksilver Gist, not in this repo. Fetch and read them before making design decisions.

- **HOLOCRON-ROUTINE-SPEC.md (v0.3)** — overall routine architecture: schedule, triage logic (7-step decision tree, 5 cadence tiers), day rollover, dormancy, source refresh detection, quality gates, failure diagnostics, concurrency/safety, proceed gate replacements.
- **HOLOCRON-RUNNER.md (v1.61)** — per-phase execution logic for the 8-phase Holocron pipeline. This is what Stage 2 translates into Python + API calls.
- **QUICKSILVER-SCHEMA.md** — JSON schema for user display files.
- **QUICKSILVER-CONTENT.md** — shared content (medals, habit categories, messaging templates). This file is the content authority for all shared text. Editorial phases must reference it. Do not invent content that belongs here.
- **NW-MENUS.md** — menu definitions.

**State vs Spec awareness:** Specs define behavior and change rarely. State files (USER-*.json, event logs, run log) change every run. Fetch specs fresh when a version bump may have occurred. Do not cache stale specs across sessions.

When in doubt about behavior, fetch the spec. Don't guess from the script comments.

## Quality Principles

- **First-time accuracy over speed.** Invest effort upfront. A correct implementation that takes longer beats a fast one that needs debugging.
- **Halt on uncertainty.** If a spec is ambiguous or a data value is unexpected, stop and surface the question rather than proceeding with assumptions.
- **Data loss prevention.** The `parse_registry()` safety check exists because the first routine run wiped all user data due to a schema mismatch. Any function that reads data and writes it back must verify it hasn't lost rows, fields, or content. If parsed output is emptier than raw input, halt.
- **Confidence Calibration.** When declaring something "fixed" or "working," distinguish between empirical evidence (test output matches expected) and reasoning alone. If reasoning only, recommend a specific test.
- **Per-user isolation.** One user's failure must never block other users' processing. Catch exceptions per-user, log diagnostics, continue to next user.
- **Post-failure diagnostics.** When any operation fails, the first question is: "What did I send, and where did it come from?" Report input values and their sources before theorizing about external causes. If the input was fabricated or inferred, say so immediately. Never attribute a failure caused by bad input to an external system.
- **Precedent is not a rule.** A prior run succeeding does not mean skip validation checks on the next run. Each run re-evaluates fresh. Data changes between runs. Specs may have been updated. The routine must never assume yesterday's success implies today's correctness.
- **Housekeeping autonomy.** Clean up stale locks, temp files, and partial writes without human intervention. The routine runs unattended. If a prior run left debris, clean it and continue.

## Bridge Coordination

This repo's work is coordinated with the Bridge project (design authority) via QB drives. Bridge owns design decisions, specs, and quality validation. Claude Code owns code execution, testing, and deployment.

**QB doc:** QB-HOLOCRON-ROUTINE.md on Quicksilver Gist (`287eb4fd487bff8f06e53bcf6cd18f2b`). Contains step plan, session history, and design decisions. The Claude Code Context section at the bottom tracks repo state for Bridge visibility.

**Protocol:**
- Steps in the QB step table are tagged `[Bridge]` or `[Claude Code]`. Execute only your tool's steps.
- After every Claude Code session, update the QB doc's Claude Code Context section (repo state, test status, open items) and check in via QB-REGISTRY.md on Bridge Gist (`7f983152470de13b03ed60bc0556957b`).
- When you need design clarification that isn't in the spec files, note it in the QB doc's Open Items. Bridge picks it up on next session.
- Bridge reads this CLAUDE.md before assessing code quality. Keep it current with repo state.

## Current State (Stage 2 First Cut)

**Stage 1 complete:** lock management, registry parsing, source fetching, 7-step triage classification, registry write-back, run log (168-entry cap), lock release.

**Stage 2 first cut (May 13, 2026):** All 8 Runner phases wired end-to-end in `holocron_runner.py`. Deterministic phases (1, 2, 3, 5d, 6, 7, 8) run in pure Python. Editorial phases (4 board meeting, 5 triggered evaluations) call the Anthropic API via the `anthropic` SDK. Quality gate scores Phase 6 output on the 6-dimension model and assigns a disposition. Ships with `HOLOCRON_DRY_RUN=true` by default — first cloud runs execute end-to-end and log intended writes without PATCHing user state, display JSONs, or registry rows. Lock acquire/release and run log writes still happen (the run log entry tags `Dry run: true`).

**Safety / config env vars (Stage 2):**
- `HOLOCRON_DRY_RUN` — default `true`. Set to `false` to enable live writes after parity validation passes.
- `HOLOCRON_USER_FILTER` — comma-separated usernames. Empty = all active users. Use for single-user smoke tests.

**Per-phase model routing:** `MODEL_CONFIG` dict at the top of `holocron_runner.py`. Defaults: Phase 4 = `claude-sonnet-4-6`, Phase 5 = `claude-sonnet-4-6`, all other phases `None` (pure Python). Phase 4 upgrade to Opus is gated on Drive 3 Trust Calibration per HOLOCRON-ROUTINE-SPEC.md §10.

**Known first-cut limitations:**
- Phase 3a++ (circle graduation) — logged as unimplemented; deferred until circles state is populated.
- Phase 3e (stack patterns) — empty; requires session-level stack grouping in event logs.
- Spark assignment in Phase 3i falls back to "first activity by creation date" if `user_state.spark_activity` is unset; the full Daily Ranking Model is not wired.
- Phase 4 collapses sub-phases 4.1-4.6 into a single Anthropic call producing nervous-system classification + one top recommendation + Worth Trying list. Per-board-member scoring expands in a later session.
- Phase 5c falls back to sequential question selection from QUICKSILVER-CONTENT.md (Question Ranking Model not yet wired).
- Quality gate scoring is mechanical (no Walker validation, no per-flag investigation tracking).

## Drive 1.5 — Baseline Runner Pipeline (Step 4 wired)

**Three-stage coupled pipeline** runs after `phase_8_write_back` for each user:

- **Stage 1 — Holocron:** `execute_pipeline()` produces `display_json` + accumulates `tokens_used` (Phase 4 is the only Anthropic call currently; deterministic phases contribute zero tokens).
- **Stage 2 — Baseline:** `baseline_runner.run_baseline_for_user()` produces a second `display_json` from the same user state, event log, and source files via a single prompt.
- **Stage 3 — Scorer:** `eval_pipeline.run_scorer()` calls `claude-sonnet-4-6` with both display JSONs + `BASELINE-ELEMENT-REGISTRY.md` + `BASELINE-SCORING-MODEL.md`. Returns per-element scores (1-5 or `"skip"`).

Lift % and zone are computed deterministically per Scoring Model formulas. Rows are appended to `EVAL-YYYY-MM.md` on the evaluation Gist (`5892d49a4d386d09a919ccae13bef709`). Per-user exceptions never propagate (per-user isolation).

**Env vars (Drive 1.5):**
- `EVAL_PIPELINE_ENABLED` — default `true`. `false` skips Stages 2-3 entirely.
- `EVAL_DRY_RUN` — default mirrors `HOLOCRON_DRY_RUN`. When `true`, the Scorer still runs and rows are staged to `/tmp/eval-rows-<user>.json` but the eval Gist PATCH is skipped.
- `EVAL_GIST_ID` — override the eval Gist ID (default `5892d49a4d386d09a919ccae13bef709`).
- `SCORER_MODEL` — override the Scorer model (default `claude-sonnet-4-6`).
- `BASELINE_MODEL` — override the Baseline model (default `claude-sonnet-4-6`).

**Manual end-to-end harness:** `python3 eval_pipeline.py <username>` runs Holocron + Stages 2-3 + eval write for one user without acquiring the routine lock — useful for validation and iteration.

**Step 5 First Live Run (May 14, 2026):** All three active users (gerber, mats, rob) scored. 51 element rows on `EVAL-2026-05.md`. Aggregate BPT lift +3,019% lands in Wide gap zone but is cost-driven (40x token gap), not quality-driven — Baseline wins 8 of 17 elements on raw score. L1 finding for Bridge: decision-zone interpretation needs calibration so cost-dominant Wide gap is distinguished from quality-dominant Wide gap. See QB-BASELINE-RUNNER.md Session 5 for full results and open items.

## Stage 2 — Validation Path (Step 4 of QB-HOLOCRON-ROUTINE.md)

Next session priorities:
1. Run `HOLOCRON_DRY_RUN=true HOLOCRON_USER_FILTER=gerber python3 holocron_runner.py` in the routine cloud. Capture stdout (would-be writes + quality gate scores).
2. Compare the would-be display JSON for gerber against the most recent manual `holocron gerber` output. Score parity against the 6-dimension model.
3. Repeat for mats, rob. Adjust phase implementations to close parity gaps.
4. When ≥ 90% parity is achieved per QB Step 4, flip `HOLOCRON_DRY_RUN=false` and let the routine write live.

**Phase overview (HOLOCRON-RUNNER.md), for reference:**
- Phase 1: Input Assembly + Phase 1b Field Initialization (deterministic)
- Phase 2: Event Ingest (deterministic — dedupe mark-off, explore, reply)
- Phase 3: 13 sub-steps (3a graduation, 3a+ badge streak, 3a++ circles, 3b target age, 3c LtL, 3d tally monthly, 3e stack patterns, 3f community medal, 3g raw dimensions, 3h fade, 3i today_view, 3j mark-off responses, 3k completion summary, 3l highlights landing, 3m together landing)
- Phase 4: Board Meeting (editorial — currently single Anthropic call fusing 4.1-4.6)
- Phase 5: Triggered Evaluations (5a rebalancing, 5b community contribution, 5c discovery question)
- Phase 5d: Content Governance Classification (Constitution routing rules)
- Phase 6: JSON Assembly (deterministic, verbatim pass-through on pre-computed values)
- Phase 7: Validation (4 passes — menus reconciliation, schema, constitution, navigation)
- Phase 8: Write-Back (deterministic — PATCH state + display JSON + clear event log, DRY_RUN guarded)

## Testing

Run locally with both tokens set (see §Environment Setup for the verification pattern):

```bash
export GITHUB_TOKEN=ghp_xxx
export ANTHROPIC_API_KEY=sk-ant-xxx
export HOLOCRON_DRY_RUN=true
export HOLOCRON_USER_FILTER=mats
python3 -m pip install -r requirements.txt
python3 holocron_runner.py
```

Baseline runner (Drive 1.5):

```bash
export GITHUB_TOKEN=ghp_xxx
export ANTHROPIC_API_KEY=sk-ant-xxx
export BASELINE_DRY_RUN=true
export BASELINE_USER_FILTER=mats
python3 baseline_runner.py
```

The deterministic phases can be smoke-tested without any tokens by constructing a `PipelineStaging` directly and calling `phase_3_field_computation`, `phase_6_json_assembly`, `phase_7_validation`, etc. — useful for offline iteration on a single user state file.

## Files in This Repo

- `holocron_runner.py` — the Holocron pipeline. Single file by design.
- `baseline_runner.py` — Baseline Runner (Drive 1.5). Single-call companion for benefit-per-token comparison.
- `eval_pipeline.py` — Drive 1.5 Step 4 pipeline glue. Stages 2 (Baseline) + 3 (Scorer) + eval Gist row append. Imported lazily from `holocron_runner.main()`.
- `requirements.txt` — dependencies (stdlib-only at Stage 1, `anthropic` SDK for Stage 2 and baseline).
- `README.md` — setup and architecture overview.
- `CLAUDE.md` — this file. Institutional memory for Claude Code sessions.
