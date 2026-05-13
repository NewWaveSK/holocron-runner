# Holocron Runner

Hourly autonomous routine that maintains personalized Quicksilver display data for all active users. Runs in the cloud via Claude Code Routines.

## Status

**Stage 1 — Scaffold (May 13, 2026).** Lock management, registry parsing, source fetching, triage, and run-log writing are implemented in `holocron_runner.py`. Pipeline phase execution and quality scoring are stubbed pending translation of `HOLOCRON-RUNNER.md` into Python + Anthropic API calls (Stage 2).

A pre-flight smoke test at Stage 1 verifies:
- The routine can authenticate with GitHub.
- It can read and write `HOLOCRON-USERS.md` (lock acquire/release).
- It correctly parses the user registry.
- It fetches all source files without error.
- It classifies each active user into a tier via the seven-step triage.
- It writes a run log entry.

What it does NOT do at Stage 1: execute Holocron Runner phases, score quality, write display JSONs.

## Environment

The routine reads two environment variables:

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | NewWaveSK token with `gist` scope. Used for all Gist reads/writes. |
| `ANTHROPIC_API_KEY` | Anthropic API key for editorial phases (Stage 2). Not used at Stage 1 — can be omitted for now. |

## Local testing

```bash
export GITHUB_TOKEN=ghp_xxx
python3 holocron_runner.py
```

The script uses only the Python standard library at Stage 1. No `pip install` required.

## Claude Code Routine setup

See `HOLOCRON-ROUTINE-SETUP.md` on the Quicksilver Gist for the full setup walkthrough. The short version:

1. Create a routine at claude.ai/code/routines with this repo attached.
2. Set the prompt to:
   ```
   Run python3 holocron_runner.py and report the final output.
   ```
3. Set environment variables `GITHUB_TOKEN` (and `ANTHROPIC_API_KEY` once Stage 2 lands).
4. Schedule: hourly (`0 * * * *`).
5. Run Now once for pre-flight. Verify lock acquire/release, triage output, run log entry.

## Architecture

```
holocron_runner.py
├── Step 1: Acquire Lock              ✓ implemented
├── Step 2: Parse User Registry       ✓ implemented
├── Step 3: Fetch Source Files        ✓ implemented
├── Step 4: Per-User Loop
│   ├── 4a Triage                     ✓ implemented (event-log checks pending)
│   ├── 4b Execute Pipeline           ⏳ stub — Stage 2
│   ├── 4c Quality Gate               ⏳ stub — Stage 2
│   └── 4d Write Back                 ⏳ stub — Stage 2
├── Step 5: Update Registry           ✓ implemented
├── Step 6: Write Run Log             ✓ implemented (168-entry cap)
├── Step 7: Release Lock              ✓ implemented (always runs)
└── Step 8: Exit                      ✓ implemented
```

## Source of truth

Specifications live on the Quicksilver Gist (`287eb4fd487bff8f06e53bcf6cd18f2b`):

- `HOLOCRON-ROUTINE-SPEC.md` — overall routine specification
- `HOLOCRON-RUNNER.md` — per-phase execution logic (to be translated in Stage 2)
- `HOLOCRON-USERS.md` — user registry and lock state
- `HOLOCRON-RUN-LOG.md` — append-only run history
