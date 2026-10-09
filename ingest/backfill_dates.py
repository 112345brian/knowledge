#!/usr/bin/env python3
"""One-time backfill of `date_added` onto the legacy fact entries (issue #35).

Until now the build filled a missing `date_added` from a constant in code
(`LEGACY_DATE_ADDED` in 04/11). This writes those values into the data so the constants can go
and a missing date can become a build error. It inserts `"date_added": "<date>"` as the first key
of every entry in pilot_facts.json, facts_batch1-4.json and general_facts.json that has none,
using the date the constant stood for (revisions.ENTRY_FILES: 2026-09-11, or 2026-09-26 for
general_facts.json), so no stored date changes. (The `measurements` client source has its own one-time
backfill for its snapshot date: `python3 -m client.backfill_snapshot_date`.)

It reuses backfill_source_keys.py's machinery, so the safety properties are the same:
  * dry run is the default; nothing is written without --apply
  * byte-preserving: the only change to a file is the inserted key; the result is verified
    (strip the insertions -> the old bytes; parse -> the old content plus the new keys)
  * idempotent; an entry that already has a `date_added` is never touched; a file with nothing to add is
    not rewritten
  * locked, atomic writes (same lock add_fact.py takes, temp file + os.replace)
  * an entry whose `date_added` is present but null, blank or not an ISO date is reported by file and index
    and NOTHING is written until you fix it by hand

Does not touch source_key (run backfill_source_keys.py for that; the two are independent).

Usage:
    python3 -m ingest.backfill_dates                       # dry run on the private data dir
    python3 -m ingest.backfill_dates --apply               # write
    python3 -m ingest.backfill_dates --data-dir DIR ...    # a copy, for trying it out
"""
import argparse
import json
import os
import sys

from ingest import backfill_rules
from ingest import backfill_source_keys
import revisions
import revisions_store
import locks

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
    return problems


def backfill_dates(data_dir, apply=False):
    """Returns {filename: entries_changed}. Raises
    revisions.RevisionError (writing nothing) on a problem entry."""
    problems = find_problems(data_dir)
    if problems:
        raise revisions.RevisionError("; ".join(f"{w}: {m}" for w, m in problems))
    return backfill_source_keys.backfill(data_dir, apply=apply, extra_adders=[date_adder], source_keys=False)


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
        print(f"  {name}: {verb} {n} entries")
    total = sum(result.values())
    if not args.apply:
        print(f"dry run: {total} change(s) needed; nothing written. Re-run with --apply.")
    else:
        print(f"{verb} {total} item(s) in {data_dir}. Review with `git -C {data_dir} diff --stat` and commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
