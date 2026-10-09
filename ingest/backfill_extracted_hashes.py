#!/usr/bin/env python3
"""One-time backfill of the fixity baseline `extracted_from_sha256` onto legacy fact entries (#38).

For each entry in pilot_facts.json and facts_batch1-4.json whose origin file is known (its own
`origin_path`, or the vault file its `notes` reference, the same rule facts.py uses), this
inserts `"extracted_from_sha256": "<hash>"` computed from the file as it is in the vault NOW. That makes
the baseline "as of backfill", NOT the true extraction time: a note edited after the fact was extracted
but before this ran is recorded as unchanged. Say so when you read an audit built on it.

Same safety properties as backfill_dates.py / backfill_source_keys.py (it reuses their machinery):
  * dry run is the default; nothing is written without --apply
  * byte-preserving: the only change to a file is the inserted key, and the result is verified
  * idempotent: an entry that already has the key is never touched
  * locked, atomic writes
An entry whose origin file is missing or unreadable is counted and skipped (no hash is invented).

Usage:
    python3 -m ingest.backfill_extracted_hashes                      # dry run on the private data dir
    python3 -m ingest.backfill_extracted_hashes --apply              # write
    python3 -m ingest.backfill_extracted_hashes --data-dir DIR ...   # a copy, for trying it out
"""
import argparse
import importlib.util
import os
import sys

from ingest import backfill_source_keys
import fixity_store
import revisions
import revisions_store

HERE = os.path.dirname(os.path.abspath(__file__))


def _resolver():
    """facts.resolve_origin_path ."""
    spec = importlib.util.spec_from_file_location("facts", os.path.join(HERE, "facts.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.resolve_origin_path


def make_adder(resolve, unresolved):
    """The `extra_adders` hook. `unresolved` collects (file, index, path-or-None) for skipped entries."""
    def adder(entry, filename, index):
        path = entry.get("origin_path") or resolve(entry.get("notes"))
        if not path:
            return {}
        sha = fixity_store.fingerprint(path)["content_sha256"]
        if sha is None:
            unresolved.append((filename, index, path))
            return {}
        return {"extracted_from_sha256": sha}
    return adder


def backfill(data_dir, apply=False, resolve=None):
    """Returns ({filename: entries_changed}, unresolved). Nothing is written unless apply=True."""
    unresolved = []
    adder = make_adder(resolve or _resolver(), unresolved)
    result = backfill_source_keys.backfill(data_dir, apply=apply, extra_adders=[adder], source_keys=False)
    return result, unresolved


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
        result, unresolved = backfill(data_dir, apply=args.apply)
    except (revisions.RevisionError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    verb = "updated" if args.apply else "would update"
    for name, n in result.items():
        print(f"  {name}: {verb} {n} entries")
    for name, index, path in unresolved:
        print(f"  skipped {name}[{index}]: origin file missing or unreadable: {path}")
    total = sum(result.values())
    print("note: baselines are hashes of the files as they are NOW (as of backfill), not at extraction time.")
    if not args.apply:
        print(f"dry run: {total} entries would get a baseline; nothing written. Re-run with --apply.")
    else:
        print(f"{verb} {total} entries in {data_dir}. Review with `git -C {data_dir} diff --stat` and commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
