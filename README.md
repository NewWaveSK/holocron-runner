# Holocron Runner

Hourly autonomous routine that maintains personalized Quicksilver display data for all active users. Runs in the cloud via Claude Code Routines.

## Status

**Stage 2 — Pipeline (May 13, 2026).** All 8 Runner phases wired end-to-end. Deterministic phases (1, 2, 3, 5d, 6, 7, 8) run in pure Python. Editorial phases (4 board meeting, 5 triggered evaluations) call the Anthropic API. Quality gate scores Phase 6 output on the 6-dimension model and assigns a disposition. Stage 2 ships with `HOLOCRON_DRY_RUN=true` by default — the first cloud runs execute end-to-end and log intended writes without PATCHing user state or display JSONs.

Stage 1 (lock management, registry parsing, source fetching, triage, run log) remains intact.

## Environment

| Variable | Purpose | Required |
|----------|---------|----------|
| `GITHUB_TOKEN` | NewWaveSK token with `gist` scope. Used for all Gist reads/writes. | Yes |
| `ANTHROPIC_API_KEY` | Anthropic API key. Required for FULL / SOURCE_REFRESH / COLD_START tiers (Phase 4-5 editorial). LIGHT-only runs can omit it. | When editorial phases run |
| `HOLOCRON_DRY_RUN` | When `true` (default in Stage 2), the routine runs end-to-end but does NOT PATCH user state, display JSONs, or registry rows. Lock acquire/release and run log writes still happen. Set to `false` to enable live writes. | No |
| `HOLOCRON_USER_FILTER` | Comma-separated usernames to process. Empty = all active users. Use for smoke tests (e.g. `HOLOCRON_USER_FILTER=mats`). | No |

## Local testing

```bash
export GITHUB_TOKEN=ghp_xxx
export ANTHROPIC_API_KEY=sk-ant-xxx          # for FULL-tier smoke tests
export HOLOCRON_DRY_RUN=true                  # default — explicit for clarity
export HOLOCRON_USER_FILTER=mats              # single-user smoke test
python3 -m pip install -r requirements.txt
python3 holocron_runner.py
```

## Claude Code Routine setup

1. Create a routine at claude.ai/code/routines with this repo attached.
2. Set the prompt to:
   ```
   Run python3 holocron_runner.py and report the final output.
   ```
3. Set environment variables `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`. For Stage 2 first activation, also set `HOLOCRON_DRY_RUN=true` and `HOLOCRON_USER_FILTER` to a single user.
4. Schedule: hourly (`0 * * * *`).
5. Run Now once. Verify lock acquire/release, triage output, quality gate scores, would-be writes in stdout, and run log entry on the Gist (the run log entry includes a `Dry run:` line).
6. Once parity validation passes (Step 4 of QB-HOLOCRON-ROUTINE.md), unset `HOLOCRON_DRY_RUN` (or set to `false`) to enable live writes.

## Architecture

```
holocron_runner.py
├── Step 1: Acquire Lock              ✓ implemented
├── Step 2: Parse User Registry       ✓ implemented
├── Step 3: Fetch Source Files        ✓ implemented
├── Step 4: Per-User Loop
│   ├── 4a Triage                     ✓ implemented (7-step decision tree)
│   ├── 4b Execute Pipeline           ✓ Stage 2 — all phases wired
│   │     ├── Phase 1   Input Assembly       ✓ deterministic
│   │     ├── Phase 1b  Field Initialization ✓ defaults applied silently
│   │     ├── Phase 2   Event Ingest         ✓ deterministic (mark-off, explore, reply)
│   │     ├── Phase 3   Mechanical Facts     ✓ 13 sub-steps (3e/3a++ stubbed)
│   │     ├── Phase 4   Board Meeting        ✓ Anthropic API (Sonnet 4.6)
│   │     ├── Phase 5   Triggered Evals      ✓ 5a/5b mechanical, 5c question pool
│   │     ├── Phase 5d  CG Classification    ✓ Constitution routing rules
│   │     ├── Phase 6   JSON Assembly        ✓ schema-compliant
│   │     ├── Phase 7   Validation           ✓ 4 passes
│   │     └── Phase 8   Write-Back           ✓ DRY_RUN guarded
│   ├── 4c Quality Gate               ✓ 6-dimension scoring (mechanical)
│   └── 4d Write Back                 ✓ dispatched per disposition
├── Step 5: Update Registry           ✓ implemented (skipped under DRY_RUN)
├── Step 6: Write Run Log             ✓ implemented (always runs)
├── Step 7: Release Lock              ✓ implemented (always runs)
└── Step 8: Exit                      ✓ implemented
```

### Model routing

`MODEL_CONFIG` at the top of `holocron_runner.py` maps phase numbers to Anthropic model IDs. Editing one line changes the model for a phase. Phase 4 upgrade to Opus is gated on Drive 3 Trust Calibration per HOLOCRON-ROUTINE-SPEC.md §10.

| Phase | Default Model |
|-------|---------------|
| 4 (Board) | `claude-sonnet-4-6` |
| 5 (Triggered Evals) | `claude-sonnet-4-6` |
| 1, 2, 3, 5d, 6, 7, 8 | None (pure Python) |

### Quality gate

Mechanical scoring across 6 dimensions (1-5) → disposition:

| Disposition | Threshold | Action |
|-------------|-----------|--------|
| CLEAN | avg ≥ 4.5 | Write JSON, register |
| FLAGGED | 3.5 ≤ avg < 4.5 | Write JSON, flag in registry notes |
| REVIEW | avg < 3.5 | Write JSON, flag REVIEW |
| CONTAMINATION | signal_isolation = 1 | Do NOT write, HALT this user |

Signal Isolation is a hard pass/fail — any cross-user data leakage in the display JSON triggers CONTAMINATION and blocks the write.

## Source of truth

Specifications live on the Quicksilver Gist (`287eb4fd487bff8f06e53bcf6cd18f2b`):

- `HOLOCRON-ROUTINE-SPEC.md` — overall routine specification (triage, cadence, quality gate)
- `HOLOCRON-RUNNER.md` — per-phase execution logic
- `QUICKSILVER-SCHEMA.md` — display JSON schema (required keys, render modes)
- `QUICKSILVER-CONTENT.md` — explore questions, affirmations, celebrations
- `NW-MENUS.md` — menu structure and breadcrumb patterns
- `HOLOCRON-USERS.md` — user registry and lock state
- `HOLOCRON-RUN-LOG.md` — append-only run history (168-entry cap)
