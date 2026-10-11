#!/usr/bin/env python3
"""One-time backfill of `measurements_snapshot.json` for the `measurements` client source (issue #35).

The vault's own db has the date each reading was taken but no timestamp for when it was loaded, so the build
stamps its rows with `synced_at` from this file instead of a constant in code. This writes
`{"synced_at": "2026-09-11"}` (the date the old constant stood for) if the file is absent.

Safety properties are those of the other backfills: dry run by default (nothing is written without --apply),
locked and atomic writes, idempotent (an existing file is never regenerated), and an unreadable or malformed
file is reported by name and nothing is written until you fix it by hand.

Usage:
    python3 -m client.backfill_snapshot_date                       # dry run on the private data dir
    python3 -m client.backfill_snapshot_date --apply               # write
    python3 -m client.backfill_snapshot_date --data-dir DIR ...    # a copy, for trying it out
"""
import argparse
import json
import os
import sys

from client.snapshot_date import MEASUREMENTS_SNAPSHOT_FILE, read_snapshot_date

# What the old `TODAY` constant in the measurements step stood for.
MEASUREMENTS_LEGACY_DATE = "2026-09-11"


def find_problems(files, inputs, data_dir):
    """[(where, what)]: the snapshot file is there but unusable."""
    problems = []
    if files.exists(os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)):
        try:
            read_snapshot_date(inputs, data_dir)
        except RuntimeError as e:
            problems.append((MEASUREMENTS_SNAPSHOT_FILE, str(e)))
    return problems


def write_snapshot(files, data_dir, apply):
    """Create measurements_snapshot.json if absent. Returns 1 if it was (or would be) written."""
    path = os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)
    with files.locked(path):
        if files.exists(path):
            return 0
        if apply:
            files.write_text_atomic(path, json.dumps({"synced_at": MEASUREMENTS_LEGACY_DATE}, indent=2) + "\n")
    return 1


def run(files, inputs, data_dir, apply=False, stdout=None, stderr=None):
    """The whole tool over `files` (a DataFiles), `inputs` (an Inputs) and `data_dir`: returns the exit code."""
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    problems = find_problems(files, inputs, data_dir)
    if problems:
        print("error: " + "; ".join(f"{w}: {m}" for w, m in problems), file=stderr)
        return 1
    try:
        n = write_snapshot(files, data_dir, apply)
    except OSError as e:
        print(f"error: {e}", file=stderr)
        return 1
    print(f"  {MEASUREMENTS_SNAPSHOT_FILE}: " + (("written" if apply else "would be written") if n else "already present, left alone"),
          file=stdout)
    if n and not apply:
        print("dry run: nothing written. Re-run with --apply.", file=stdout)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="Write the file (default is a dry run).")
    p.add_argument("--dry-run", action="store_true", help="Explicit no-op flag; dry run is already the default.")
    p.add_argument("--data-dir", help="Directory holding measurements_snapshot.json (default: the private data dir).")
    args = p.parse_args(argv)
    if args.apply and args.dry_run:
        p.error("--apply and --dry-run are mutually exclusive")
    from container import Container
    app = Container()
    data_dir = args.data_dir or app.locations.private_data_dir
    return run(app.datafiles, app.inputs, data_dir, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
