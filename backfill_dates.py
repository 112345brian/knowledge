#!/usr/bin/env python3
"""One-time backfill of `date_added` onto the legacy fact entries (issue #35).

Until now the build filled a missing `date_added` from a constant in code
(`LEGACY_DATE_ADDED` in 04/11). This writes those values into the data so the constants can go
and a missing date can become a build error. It inserts `"date_added": "<date>"` as the first key
of every entry in pilot_facts.json, facts_batch1-4.json and general_facts.json that has none,
using the date the constant stood for (revisions.ENTRY_FILES: 2026-09-11, or 2026-09-26 for
general_facts.json), so no stored date changes. It also writes measurements_snapshot.json
(`{"synced_at": "2026-09-11"}`) if absent: the date 03_ingest_measurements.py stamps on its rows,
for the same reason (the vault db has no per-row load timestamp).

It reuses backfill_source_keys.py's machinery, so the safety properties are the same:
  * dry run is the default; nothing is written without --apply
  * byte-preserving: the only change to a file is the inserted key; the result is verified
    (strip the insertions -> the old bytes; parse -> the old content plus the new keys)
  * idempotent; an entry that already has a `date_added`, or a snapshot file that exists, is never
    touched or regenerated; a file with nothing to add is not rewritten
  * locked, atomic writes (same lock add_fact.py takes, temp file + os.replace)
  * an entry whose `date_added` is present but null, blank or not an ISO date, or an unreadable
    snapshot file, is reported by file and index and NOTHING is written until you fix it by hand

Does not touch source_key (run backfill_source_keys.py for that; the two are independent).

Usage:
    python3 backfill_dates.py                       # dry run on the private data dir
    python3 backfill_dates.py --apply               # write
    python3 backfill_dates.py --data-dir DIR ...    # a copy, for trying it out
"""
import argparse
import json
import os
import sys

import backfill_rules
import backfill_source_keys
import revisions
import revisions_store
from add_fact_store import file_lock, lock_path
from snapshot_date import MEASUREMENTS_SNAPSHOT_FILE, read_snapshot_date

# What 03_ingest_measurements.py's `TODAY` constant stood for.
MEASUREMENTS_LEGACY_DATE = "2026-09-11"
FILE_DATES = dict(revisions.ENTRY_FILES)


def date_adder(entry, filename, index):
    """The `extra_adders` hook: the file's legacy date. (backfill skips entries that have the key.)"""
    return {"date_added": FILE_DATES[filename]}


def find_problems(data_dir):
    """Entries/files that can't be backfilled safely. [(where, what)]"""
    problems = []
    for name, _ in revisions.ENTRY_FILES:
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            continue
        problems.extend(backfill_rules.date_problems(name, revisions_store.read_array(path)))
    if os.path.exists(os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)):
        try:
            read_snapshot_date(data_dir)
        except RuntimeError as e:
            problems.append((MEASUREMENTS_SNAPSHOT_FILE, str(e)))
    return problems


def _write_snapshot(data_dir, apply):
    """Create measurements_snapshot.json if absent. Returns 1 if it was (or would be) written."""
    path = os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)
    with file_lock(lock_path(path)):
        if os.path.exists(path):
            return 0
        if apply:
            revisions_store.atomic_write_text(path, json.dumps({"synced_at": MEASUREMENTS_LEGACY_DATE}, indent=2) + "\n")
    return 1


def backfill_dates(data_dir, apply=False):
    """Returns {filename: entries_changed, MEASUREMENTS_SNAPSHOT_FILE: 0|1}. Raises
    revisions.RevisionError (writing nothing) on a problem entry."""
    problems = find_problems(data_dir)
    if problems:
        raise revisions.RevisionError("; ".join(f"{w}: {m}" for w, m in problems))
    result = backfill_source_keys.backfill(data_dir, apply=apply, extra_adders=[date_adder], source_keys=False)
    result[MEASUREMENTS_SNAPSHOT_FILE] = _write_snapshot(data_dir, apply)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="Write the changes (default is a dry run).")
    p.add_argument("--dry-run", action="store_true", help="Explicit no-op flag; dry run is already the default.")
    p.add_argument("--data-dir", help="Directory holding the fact JSON files (default: the private data dir).")
    args = p.parse_args(argv)
    if args.apply and args.dry_run:
        p.error("--apply and --dry-run are mutually exclusive")
    data_dir = args.data_dir or revisions_store.default_data_dir()
    try:
        result = backfill_dates(data_dir, apply=args.apply)
    except (revisions.RevisionError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    verb = "updated" if args.apply else "would update"
    for name, n in result.items():
        if name == MEASUREMENTS_SNAPSHOT_FILE:
            print(f"  {name}: " + (("written" if args.apply else "would be written") if n else "already present, left alone"))
        else:
            print(f"  {name}: {verb} {n} entries")
    total = sum(result.values())
    if not args.apply:
        print(f"dry run: {total} change(s) needed; nothing written. Re-run with --apply.")
    else:
        print(f"{verb} {total} item(s) in {data_dir}. Review with `git -C {data_dir} diff --stat` and commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
