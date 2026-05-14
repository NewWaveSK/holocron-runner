#!/usr/bin/env python3
"""
Baseline Runner — offline test scaffold for holocron_runner.py.

Runs holocron_runner.main() end-to-end against frozen fixture files instead of
live Gists. Every gist read is served from disk; every gist PATCH is captured
to disk. No HTTP. Useful for:

  - Reproducing a cloud run locally without GITHUB_TOKEN.
  - Diffing the routine's would-be display JSON against a known-good baseline.
  - Smoke-testing changes to deterministic phases without burning API quota.

Fixture layout (mirrors the two source Gists):

  <fixtures>/
    quicksilver/                       # gist 287eb4fd487bff8f06e53bcf6cd18f2b
      HOLOCRON-USERS.md
      HOLOCRON-RUN-LOG.md
      HOLOCRON-RUNNER.md
      HOLOCRON-ROUTINE-SPEC.md
      QUICKSILVER-SCHEMA.md
      QUICKSILVER-CONTENT.md
      QUICKSILVER-CONSTITUTION.md
      NW-MENUS.md
      QUICKSILVER-<user>.json          # optional — used as parity baseline
      QUICKSILVER-<user>-LOG.json      # optional — event log; absent = empty
    nw_state/                          # gist 961083278c6a59b863314e56c5a60402
      USER-<user>.json

Usage:

  python3 tests/baseline_runner.py --fixtures tests/fixtures --user gerber

Output (default tests/baseline-output/<timestamp>/):

  stdout.log              # captured runner stdout
  patches.jsonl           # one JSON record per gist_patch_files call
  captured/quicksilver/   # files the runner would have written to Quicksilver
  captured/nw_state/      # files the runner would have written to NW State
  parity/<user>.md        # bucket diff vs baseline (if baseline present)
  summary.json            # run-level summary (counts, diagnostics)

Exit code mirrors holocron_runner.main() (0 = clean).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
QUICKSILVER_GIST_ID = "287eb4fd487bff8f06e53bcf6cd18f2b"
NW_STATE_GIST_ID = "961083278c6a59b863314e56c5a60402"
GIST_DIRS = {QUICKSILVER_GIST_ID: "quicksilver", NW_STATE_GIST_ID: "nw_state"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixtures", type=Path, default=REPO_ROOT / "tests" / "fixtures",
                   help="Directory containing quicksilver/ and nw_state/ subdirs (default: tests/fixtures)")
    p.add_argument("--user", default="",
                   help="Comma-separated usernames to process (passes through as HOLOCRON_USER_FILTER). Empty = all active.")
    p.add_argument("--output-dir", type=Path, default=None,
                   help="Where to write captures (default: tests/baseline-output/<utc-timestamp>/)")
    p.add_argument("--dry-run", action="store_true",
                   help="Set HOLOCRON_DRY_RUN=true so Phase 8 short-circuits before writing display/state. "
                        "By default this harness runs with DRY_RUN=false and captures every PATCH instead.")
    return p.parse_args()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def validate_fixtures(fixtures: Path, output_dir: Path) -> None:
    if not fixtures.is_dir():
        sys.exit(f"Fixtures directory not found: {fixtures}\n"
                 f"This script reads frozen Gist files from disk. Drop the Quicksilver Gist\n"
                 f"files into {fixtures}/quicksilver/ and the NW State Gist files into\n"
                 f"{fixtures}/nw_state/ — see this script's docstring for the layout.")
    missing = [name for name in ("quicksilver", "nw_state") if not (fixtures / name).is_dir()]
    if missing:
        sys.exit(f"Fixtures dir {fixtures} is missing required subdir(s): {missing}")
    # Output dir is created lazily by ensure_dir below.
    output_dir.mkdir(parents=True, exist_ok=True)


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def build_mocks(fixtures: Path, output_dir: Path):
    """
    Returns (gist_get_mock, gist_patch_mock, patches_record).
    patches_record is the list reference into which every PATCH is appended.
    """
    patches_record: list[dict] = []
    captured_root = ensure_dir(output_dir / "captured")

    def gist_get_mock(gist_id: str) -> dict:
        subdir = GIST_DIRS.get(gist_id)
        if subdir is None:
            raise RuntimeError(f"baseline_runner: unmocked gist_id {gist_id!r}")
        gist_dir = fixtures / subdir
        if not gist_dir.is_dir():
            raise FileNotFoundError(f"baseline_runner: fixture subdir missing: {gist_dir}")
        files = {}
        latest_mtime = 0.0
        for entry in sorted(gist_dir.iterdir()):
            if not entry.is_file():
                continue
            files[entry.name] = {"content": entry.read_text(encoding="utf-8")}
            mtime = entry.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
        updated_at = datetime.fromtimestamp(latest_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") \
            if latest_mtime else "1970-01-01T00:00:00Z"
        return {"files": files, "updated_at": updated_at}

    def gist_patch_mock(gist_id: str, files: dict[str, str]) -> dict:
        subdir = GIST_DIRS.get(gist_id, f"unknown-{gist_id}")
        out_subdir = ensure_dir(captured_root / subdir)
        sizes = {}
        for fname, content in files.items():
            (out_subdir / fname).write_text(content, encoding="utf-8")
            sizes[fname] = len(content)
        patches_record.append({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "gist_id": gist_id,
            "subdir": subdir,
            "files": list(files.keys()),
            "sizes": sizes,
        })
        return {"history": [{"version": "baseline-runner-mock"}]}

    return gist_get_mock, gist_patch_mock, patches_record


def block_network() -> None:
    """Replace urlopen so any unintended HTTP call fails loudly."""
    import urllib.request

    def _blocked(*args, **kwargs):
        raise RuntimeError("baseline_runner: network access is blocked in offline harness")

    urllib.request.urlopen = _blocked  # type: ignore[assignment]


class Tee(io.TextIOBase):
    """Mirror writes to two text streams (the real stdout and a file)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
            st.flush()
        return len(s)

    def flush(self):
        for st in self._streams:
            st.flush()


def parity_report(captured_display: dict, baseline_display: dict) -> str:
    captured_keys = set(captured_display.keys())
    baseline_keys = set(baseline_display.keys())

    common = sorted(captured_keys & baseline_keys)
    only_captured = sorted(captured_keys - baseline_keys)
    only_baseline = sorted(baseline_keys - captured_keys)

    identical, differ = [], []
    for k in common:
        if json.dumps(captured_display[k], sort_keys=True) == json.dumps(baseline_display[k], sort_keys=True):
            identical.append(k)
        else:
            differ.append(k)

    lines = [
        "# Parity vs baseline display",
        "",
        f"- Baseline keys: {len(baseline_keys)}",
        f"- Captured keys: {len(captured_keys)}",
        "",
        f"| Bucket | Count | Keys |",
        f"|---|---|---|",
        f"| Common, identical | {len(identical)} | {', '.join(f'`{k}`' for k in identical) or '—'} |",
        f"| Common, differ | {len(differ)} | {', '.join(f'`{k}`' for k in differ) or '—'} |",
        f"| Baseline only | {len(only_baseline)} | {', '.join(f'`{k}`' for k in only_baseline) or '—'} |",
        f"| Captured only | {len(only_captured)} | {', '.join(f'`{k}`' for k in only_captured) or '—'} |",
        "",
    ]
    return "\n".join(lines)


def write_parity_reports(fixtures: Path, output_dir: Path, patches_record: list[dict]) -> list[str]:
    """For each captured QUICKSILVER-<user>.json, diff against the same-named baseline if present."""
    qs_capture_dir = output_dir / "captured" / "quicksilver"
    qs_fixture_dir = fixtures / "quicksilver"
    if not qs_capture_dir.is_dir():
        return []
    parity_dir = ensure_dir(output_dir / "parity")
    written = []
    for f in sorted(qs_capture_dir.glob("QUICKSILVER-*.json")):
        if f.name.endswith("-LOG.json"):
            continue
        baseline_path = qs_fixture_dir / f.name
        if not baseline_path.exists():
            continue
        try:
            captured = json.loads(f.read_text(encoding="utf-8"))
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            (parity_dir / f"{f.stem}.md").write_text(f"# Parity: parse error\n\n{e}\n", encoding="utf-8")
            continue
        if not isinstance(captured, dict) or not isinstance(baseline, dict):
            continue
        report = parity_report(captured, baseline)
        report_path = parity_dir / f"{f.stem}.md"
        report_path.write_text(report, encoding="utf-8")
        written.append(str(report_path.relative_to(output_dir)))
    return written


def main() -> int:
    args = parse_args()
    fixtures = args.fixtures.resolve()
    output_dir = (args.output_dir or REPO_ROOT / "tests" / "baseline-output" / stamp()).resolve()

    validate_fixtures(fixtures, output_dir)

    # Env must be set BEFORE importing holocron_runner — module captures these
    # at load time (GITHUB_TOKEN, HOLOCRON_DRY_RUN, HOLOCRON_USER_FILTER).
    os.environ["GITHUB_TOKEN"] = "baseline-runner-sentinel"
    os.environ["HOLOCRON_DRY_RUN"] = "true" if args.dry_run else "false"
    if args.user:
        os.environ["HOLOCRON_USER_FILTER"] = args.user
    else:
        os.environ.pop("HOLOCRON_USER_FILTER", None)

    anthropic_present = bool(os.environ.get("ANTHROPIC_API_KEY"))

    sys.path.insert(0, str(REPO_ROOT))
    block_network()
    import holocron_runner

    gist_get_mock, gist_patch_mock, patches_record = build_mocks(fixtures, output_dir)
    holocron_runner.gist_get = gist_get_mock
    holocron_runner.gist_patch_files = gist_patch_mock

    log_path = output_dir / "stdout.log"
    tee = Tee(sys.stdout, log_path.open("w", encoding="utf-8"))

    print(f"baseline_runner: fixtures={fixtures}", file=tee)
    print(f"baseline_runner: output_dir={output_dir}", file=tee)
    print(f"baseline_runner: dry_run={args.dry_run} user_filter={args.user or 'all'} "
          f"anthropic_key={'set' if anthropic_present else 'unset'}", file=tee)
    if not anthropic_present:
        print("baseline_runner: ANTHROPIC_API_KEY unset — editorial phases 4/5 will short-circuit", file=tee)

    original_stdout = sys.stdout
    sys.stdout = tee
    rc = 1
    error: str | None = None
    try:
        rc = holocron_runner.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except Exception:
        error = traceback.format_exc()
        print(error, file=tee)
    finally:
        sys.stdout = original_stdout

    # Persist captured patches.
    with (output_dir / "patches.jsonl").open("w", encoding="utf-8") as fh:
        for entry in patches_record:
            fh.write(json.dumps(entry) + "\n")

    parity_files = write_parity_reports(fixtures, output_dir, patches_record)

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "fixtures": str(fixtures),
        "output_dir": str(output_dir),
        "dry_run": args.dry_run,
        "user_filter": args.user,
        "anthropic_key_present": anthropic_present,
        "runner_exit_code": rc,
        "patches_captured": len(patches_record),
        "patches_by_subdir": _count_by(patches_record, "subdir"),
        "diagnostics": [d.get("kind") for d in getattr(holocron_runner, "DIAGNOSTICS", [])],
        "parity_reports": parity_files,
        "error": error,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"\nbaseline_runner: wrote {len(patches_record)} captured PATCH(es) to {output_dir}/captured/")
    if parity_files:
        print(f"baseline_runner: parity reports: {', '.join(parity_files)}")
    print(f"baseline_runner: summary -> {output_dir}/summary.json")
    return rc


def _count_by(records: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in records:
        v = r.get(key, "")
        out[v] = out.get(v, 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main())
