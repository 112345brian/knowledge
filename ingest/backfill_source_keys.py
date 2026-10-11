#!/usr/bin/env python3
"""One-time backfill of `source_key` onto the legacy fact entries (issue #30).

Adds `"source_key": "legacy-..."` as the first key of every entry in pilot_facts.json,
facts_batch1-4.json and general_facts.json that has none. That insertion is the only edit:
every other byte of each file is preserved (indentation, key order, spacing, trailing newline),
and the result is verified before anything is written. The keys are the same ones the build
derives in memory (`revisions.derive_keys`), so building before or after gives identical
fact_revisions.

Safe by construction:
  * dry run is the default; nothing is written without --apply
  * idempotent: entries that already have a key are never touched or regenerated, and a file
    with nothing to add is not rewritten at all
  * each file is rewritten under the same inter-process lock add_fact.py uses (so a concurrent
    add_fact append cannot be lost), via a temp file + os.replace (atomic)
  * after rewriting, the new text is parsed and compared: removing the added keys must give
    exactly the old parsed content, and removing the inserted text must give the old bytes

Usage:
    python3 -m ingest.backfill_source_keys                       # dry run on the private data dir
    python3 -m ingest.backfill_source_keys --apply               # write
    python3 -m ingest.backfill_source_keys --data-dir DIR ...    # a copy, for trying it out

Extension point (used by backfill_dates.py, #35): pass `extra_adders` to `backfill()` -- each is
a callable (entry, filename, index) -> {key: value} of keys to insert for that entry; keys the
entry already has are skipped. `source_keys=False` turns the source_key pass off.
"""
import argparse
import os
import sys

from ingest import backfill_rules
import revisions
from ingest.backfill_rules import rewrite_text, source_key_adder  # noqa: F401  (the public API)


def _backfill_file(files, path, name, adders, apply):
    """Returns the number of entries changed in this file."""
    with files.locked(path):
        text = files.read_text(path, newline="")
        items = files.read_array(path)
        keys = revisions.derive_keys(items, name)
        context = {name: keys}
        additions = backfill_rules.plan_additions(items, name, adders(context))
        changed = sum(1 for p in additions if p)
        if changed and apply:
            new, _ = rewrite_text(text, path, additions)
            files.write_text_atomic(path, new)
        elif changed:
            rewrite_text(text, path, additions)  # dry run still proves the rewrite would verify
    return changed


def backfill(files, data_dir, apply=False, extra_adders=(), source_keys=True):
    """Backfill source_key (when `source_keys`) and whatever `extra_adders` return across the
    legacy files. Returns {filename: entries_changed}. Nothing is written unless apply=True."""
    # All keys must be unique across files before we write anything.
    files.load_entries(data_dir)
    result = {}
    for name, _ in revisions.ENTRY_FILES:
        path = os.path.join(data_dir, name)
        if not files.exists(path):
            continue
        result[name] = _backfill_file(files, path, name,
                                      lambda ctx: [*([source_key_adder(ctx)] if source_keys else []), *extra_adders], apply)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="Write the changes (default is a dry run).")
    p.add_argument("--dry-run", action="store_true", help="Explicit no-op flag; dry run is already the default.")
    p.add_argument("--data-dir", help="Directory holding the fact JSON files (default: the private data dir).")
    args = p.parse_args(argv)
    if args.apply and args.dry_run:
        p.error("--apply and --dry-run are mutually exclusive")
    import local_datafiles
    import paths
    data_dir = args.data_dir or paths.load().private_data_dir
    try:
        result = backfill(local_datafiles, data_dir, apply=args.apply)
    except (revisions.RevisionError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    verb = "updated" if args.apply else "would update"
    for name, n in result.items():
        print(f"  {name}: {verb} {n} entries")
    total = sum(result.values())
    if not args.apply:
        print(f"dry run: {total} entries need a source_key; nothing written. Re-run with --apply.")
    else:
        print(f"{verb} {total} entries in {data_dir}. Review with `git -C {data_dir} diff --stat` and commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
