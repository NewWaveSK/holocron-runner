# Validation Findings — Holocron Runner Stage 2, gerber dry-run

**Branch:** `claude/validate-holocron-gerber-KoW3N` (fast-forwarded to Stage 2 head `4687826`).
**Date:** 2026-05-13
**Scope:** QB-HOLOCRON-ROUTINE Step 4 (Validation). User filter: `gerber`.

## What was run

Offline smoke harness (`/tmp/harness.py`, not committed) — monkey-patches `gist_get`, `gist_get_file`, `gist_patch_files` against cached Gist files in `/tmp/baseline/`. Real `holocron_runner.main()` invoked with `HOLOCRON_DRY_RUN=true`, `HOLOCRON_USER_FILTER=gerber`. No `ANTHROPIC_API_KEY` (sandbox limitation), so editorial phases 4/5 short-circuit.

Cached inputs from raw Gist URLs:
- Quicksilver Gist `287eb4fd487bff8f06e53bcf6cd18f2b`: `HOLOCRON-USERS.md`, `HOLOCRON-RUN-LOG.md`, `HOLOCRON-RUNNER.md`, `HOLOCRON-ROUTINE-SPEC.md`, `QUICKSILVER-SCHEMA.md`, `QUICKSILVER-CONTENT.md`, `NW-MENUS.md`, `QUICKSILVER-CONSTITUTION.md`, `QUICKSILVER-GERBER.json` (baseline display).
- NW State Gist `961083278c6a59b863314e56c5a60402`: `USER-gerber.json`.
- `QUICKSILVER-gerber-LOG.json` returned 404 → empty event log (graceful per Phase 1 code path).

## Pipeline health

- Imports clean, runs end-to-end, exit code 0.
- Triage: `gerber → SOURCE_REFRESH` (correct — registry has empty `last_source_versions`).
- Phases executed: 1, 2 (0 events), 3, 4 (no-op without key), 5 (no-op without key), 5d, 6, 7.
- Phase 7 validation: **passed, zero failures**.
- DRY_RUN guards behave correctly. Captured PATCH calls were exactly:
  1. `HOLOCRON-USERS.md` (lock acquire)
  2. `HOLOCRON-RUN-LOG.md` (observability)
  3. `HOLOCRON-USERS.md` (lock release)
  No user state or display JSON writes attempted.

## Anthropic-key dependency

Phases 4 and 5 set `s.phase_errors[4] = "anthropic client unavailable"` when the key is absent. `execute_pipeline` does **not** raise on this (only on Python exceptions), so phases 5, 5d, 6, 7 still run. After return, `main()` (holocron_runner.py:2178-2180) sees `phase_errors` non-empty, appends `PHASE_ERR ([4])`, and `continue`s — skipping quality scoring and Phase 8.

Net effect: without `ANTHROPIC_API_KEY`, the cloud dry-run produces nothing to compare against the baseline. Step 4 sign-off **requires the cloud run to have both `GITHUB_TOKEN` and `ANTHROPIC_API_KEY` set**.

## Parity vs baseline `QUICKSILVER-GERBER.json`

| Bucket | Count | Keys |
|---|---|---|
| Common, identical | 3 | `home`, `progress_groups`, `today` |
| Common, differ | 11 | `explore`, `more`, `progress`, `progress_signals`, `progress_spark`, `progress_worth_trying`, `spark`, `spark_done`, `together`, `together_connections`, `together_messaging` |
| Baseline only | 10 | `onboarding`, `more_lifeadmin_done`, `stack_weightlifting`, `stack_wl_done_dbpress`, `stack_wl_done_hlr`, `stack_wl_done_latraises`, `stack_wl_done_ohp`, `stack_wl_done_pullups`, `stack_wl_done_squats`, `stack_wl_exit` |
| Dry-run only | 2 | `more_done_outdoor_walk`, `more_done_weight_lifting` |

### Explanations

**11 common-but-differ:** All depend on board output (Phase 4) and triggered evals (Phase 5). Without Anthropic, Phase 6 falls back to template/default content for `explore` queue, Worth Trying suggestions, spark inline copy, together messaging, etc. Cannot be parity-tested offline.

**Baseline-only `stack_weightlifting` + 6× `stack_wl_done_*` + `stack_wl_exit` (8 keys):** Phase 3e (stack patterns) is explicitly **deferred** per QB doc Known Limitations. Not a regression introduced by Stage 2.

**Baseline-only `onboarding`:** gerber.`onboarding_complete = True` in user state. QUICKSILVER-SCHEMA.md line 120 — "NWR removes the `onboarding` key after processing answers at first refresh." Dry-run omission is **per spec**; the baseline retention is the stale state.

**Dry-run-only `more_done_{slug}` keys vs baseline `more_{slug}_done` / `stack_wl_done_*`:** Naming convention mismatch. Script (holocron_runner.py:1659, 1662) hardcodes `more_done_{slug}`. NW-MENUS.md is silent on JSON key naming. **Bridge design question** — needs a ruling before this can be called a parity gap (vs an intentional Stage 2 simplification).

## Blockers for Step 4 sign-off

1. Cloud dry-run with both tokens set. This sandbox has no Routines trigger and no tokens.
2. Branch routing — cloud routine likely points at `main`. Either repoint at `claude/validate-holocron-gerber-KoW3N` (which is the Stage 2 head + this doc) or merge Stage 2 → `main` first.
3. Bridge ruling on activity-menu JSON-key naming (`more_done_{slug}` vs `more_{slug}_done`).
4. Confirm `stack_*` absence is acceptable for Step 4 sign-off given Phase 3e is deferred (or undefer it in scope).

## What I did NOT change

No code edits. Reasoning per CLAUDE.md "Halt on uncertainty" + "Don't add features beyond what the task requires":
- The menu-naming pattern is hardcoded in Phase 6; changing it is a Bridge call, not a code-quality call.
- Phase 3e (stack patterns) is roadmap-deferred; rebuilding it expands scope.
- The `phase_errors`-without-raise pattern in `execute_pipeline` is consistent — phases that need the editorial output of 4/5 are downstream and would themselves error; `main()`'s short-circuit on `phase_errors` is the intended gate.

## Reproducing the offline harness

The harness file lives at `/tmp/harness.py` (sandbox-local, not committed). Conceptually:

1. Cache the 11 input files into a baseline directory.
2. Set `GITHUB_TOKEN` to any non-empty value (passes the early check in `main()`).
3. Monkey-patch `holocron_runner.gist_get` / `gist_get_file` to read from the baseline dir, and `gist_patch_files` to capture-and-no-op.
4. Call `holocron_runner.main()`.
5. Inspect `staging.display_json`, `staging.phase_errors`, `staging.validation_failures`, and the captured PATCH list.

Future sessions: if a permanent offline test scaffold is desired, lift this into `tests/` with the baseline files as fixtures.
