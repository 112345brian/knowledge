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

import locks
import revisions_store
from client.snapshot_date import MEASUREMENTS_SNAPSHOT_FILE, read_snapshot_date

# What the old `TODAY` constant in the measurements step stood for.
MEASUREMENTS_LEGACY_DATE = "2026-09-11"


def find_problems(data_dir):
    """[(where, what)]: the snapshot file is there but unusable."""
    problems = []
    if os.path.exists(os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)):
        try:
            read_snapshot_date(data_dir)
        except RuntimeError as e:
            problems.append((MEASUREMENTS_SNAPSHOT_FILE, str(e)))
    return problems


def write_snapshot(data_dir, apply):
    """Create measurements_snapshot.json if absent. Returns 1 if it was (or would be) written."""
    path = os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)
    with locks.file_lock(locks.lock_path(path)):
        if os.path.exists(path):
            return 0
        if apply:
            revisions_store.atomic_write_text(path, json.dumps({"synced_at": MEASUREMENTS_LEGACY_DATE}, indent=2) + "\n")
    return 1


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="Write the file (default is a dry run).")
    p.add_argument("--dry-run", action="store_true", help="Explicit no-op flag; dry run is already the default.")
    p.add_argument("--data-dir", help="Directory holding measurements_snapshot.json (default: the private data dir).")
    args = p.parse_args(argv)
    if args.apply and args.dry_run:
        p.error("--apply and --dry-run are mutually exclusive")
    data_dir = args.data_dir or revisions_store.default_data_dir()
    problems = find_problems(data_dir)
    if problems:
        print("error: " + "; ".join(f"{w}: {m}" for w, m in problems), file=sys.stderr)
        return 1
    try:
        n = write_snapshot(data_dir, args.apply)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"  {MEASUREMENTS_SNAPSHOT_FILE}: " + (("written" if args.apply else "would be written") if n else "already present, left alone"))
    if n and not args.apply:
        print("dry run: nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
