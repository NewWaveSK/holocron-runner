# CLAUDE.md — Holocron Runner

## What This Is

Hourly autonomous routine that maintains personalized Quicksilver display data for all active users. Runs via Claude Code Routines (cloud). The Python script (`holocron_runner.py`) handles all deterministic orchestration. Editorial phases (4-5) call the Anthropic API for content generation.

## Security — Non-Negotiable

- **Never write tokens into any file that gets committed or pushed.** Tokens come from environment variables only (`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`).
- **Pre-commit scan:** Before any commit, verify no string matching `ghp_` followed by 20+ alphanumeric characters appears in any staged file. GitHub automatically revokes leaked tokens.
- **No fabricated data.** Every credential, ID, URL, and filename must trace to a named source (environment variable, API response, spec file). If a value is unknown, halt and report the gap. Never infer or guess.

## Gist Operations

All Quicksilver data lives on GitHub Gists, not in this repo. The script reads and writes Gists via the GitHub API using `GITHUB_TOKEN`.

**Key Gist:** Quicksilver Gist `287eb4fd487bff8f06e53bcf6cd18f2b` — hosts user registry, run log, source files (schema, content, menus, runner, explore sets), and per-user display JSONs.

**NW State Gist:** `961083278c6a59b863314e56c5a60402` — hosts per-user state files (USER-gerber.json, USER-mats.json) and event logs.

**Pattern:** Fetch full Gist in one API call → extract files by name → process → PATCH only changed files back. Never inline large payloads in curl `-d` strings. Write to temp file and use `-d @path`.

## Source of Truth — Specs on the Gist

These files define WHAT to build. They live on the Quicksilver Gist, not in this repo. Fetch and read them before making design decisions.

- **HOLOCRON-ROUTINE-SPEC.md (v0.3)** — overall routine architecture: schedule, triage logic (7-step decision tree, 5 cadence tiers), day rollover, dormancy, source refresh detection, quality gates, failure diagnostics, concurrency/safety, proceed gate replacements.
- **HOLOCRON-RUNNER.md (v1.61)** — per-phase execution logic for the 8-phase Holocron pipeline. This is what Stage 2 translates into Python + API calls.
- **QUICKSILVER-SCHEMA.md** — JSON schema for user display files.
- **QUICKSILVER-CONTENT.md** — shared content (medals, habit categories, messaging templates).
- **NW-MENUS.md** — menu definitions.

When in doubt about behavior, fetch the spec. Don't guess from the script comments.

## Quality Principles

- **First-time accuracy over speed.** Invest effort upfront. A correct implementation that takes longer beats a fast one that needs debugging.
- **Halt on uncertainty.** If a spec is ambiguous or a data value is unexpected, stop and surface the question rather than proceeding with assumptions.
- **Data loss prevention.** The `parse_registry()` safety check exists because the first routine run wiped all user data due to a schema mismatch. Any function that reads data and writes it back must verify it hasn't lost rows, fields, or content. If parsed output is emptier than raw input, halt.
- **Confidence Calibration.** When declaring something "fixed" or "working," distinguish between empirical evidence (test output matches expected) and reasoning alone. If reasoning only, recommend a specific test.
- **Per-user isolation.** One user's failure must never block other users' processing. Catch exceptions per-user, log diagnostics, continue to next user.

## Current State (Stage 1 Complete)

Implemented: lock management, registry parsing, source fetching, 7-step triage classification, registry write-back, run log (168-entry cap), lock release.

Stubbed (Stage 2): pipeline phase execution (4b), quality scoring (4c), JSON write-back (4d).

## Stage 2 — What to Build

Translate the 8 Holocron Runner phases into the per-user pipeline. For each phase:

1. Read the phase spec in HOLOCRON-RUNNER.md
2. Classify: **deterministic** (pure data transformation, Python) or **editorial** (content generation, Anthropic API call)
3. Implement with mechanical quality gates replacing the interactive proceed gates described in the runner

**Phase overview (from HOLOCRON-RUNNER.md):**
- Phase 1: Source Assembly (deterministic — gather inputs)
- Phase 2: Event Extraction (deterministic — parse event log)
- Phase 3: Field Computation (deterministic — 13 sub-steps covering streaks, medals, schedules, today view, etc.)
- Phase 4: Board Scoring (editorial — board members score dimensions)
- Phase 5: Suggestion Generation (editorial — produce suggestions from scores)
- Phase 5d: Display Assembly (deterministic — build final JSON structure)
- Phase 6: Relationship Scan (deterministic — verify internal consistency)
- Phase 7: Quality Gate (deterministic — 6-dimension scoring model)
- Phase 8: Write-Back (deterministic — PATCH JSON to Gist if quality passes)

**Model routing (future):** Phases 4-5 will eventually use Opus for editorial quality. All other phases use Sonnet or pure Python. The `MODEL_CONFIG` block in the script supports per-phase model selection. Default all to Sonnet initially.

## Testing

Run locally with `GITHUB_TOKEN` set:
```bash
export GITHUB_TOKEN=ghp_xxx
python3 holocron_runner.py
```

For development, test single users by temporarily filtering in the per-user loop. Compare output against the most recent manual `holocron` run for that user (check HOLOCRON-USERS.md for last run details).

## Files in This Repo

- `holocron_runner.py` — the script. Single file by design.
- `requirements.txt` — dependencies (stdlib-only at Stage 1, `anthropic` SDK for Stage 2).
- `README.md` — setup and architecture overview.
- `CLAUDE.md` — this file. Institutional memory for Claude Code sessions.
