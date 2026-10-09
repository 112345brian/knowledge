#!/usr/bin/env python3
"""One-time export of the built-in subject hierarchy to `subjects.json` in knowledge-private (#43).

Until now step 06 hardcoded which subjects sit under `anabolic-steroids` and `training`. This writes
exactly that hierarchy (26 entries: the two parents and their 24 children, relation `broader`) as data,
so it can be edited with `knowledge.py subject ...` instead of code. The resulting database is the same:
the tests build with the built-in table and with this file and compare the subject rows.

  * dry run is the default: prints what it would write and touches nothing
  * --apply writes the file atomically; it refuses to overwrite an existing subjects.json
  * --data-dir DIR tries it on a copy
Commit the result in knowledge-private yourself (or let `subject ...` commits start from it).

Usage:
    python3 export_subjects.py              # dry run
    python3 export_subjects.py --apply
"""
import argparse
import importlib.util
import os
import sys

import subjects

HERE = os.path.dirname(os.path.abspath(__file__))


def builtin_entries():
    spec = importlib.util.spec_from_file_location("seed_subject_hierarchy", os.path.join(HERE, "06_seed_subject_hierarchy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.builtin_entries()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="Write the file (default is a dry run).")
    p.add_argument("--dry-run", action="store_true", help="Explicit no-op flag; dry run is already the default.")
    p.add_argument("--data-dir", help="Directory to write subjects.json into (default: the private data dir).")
    args = p.parse_args(argv)
    if args.apply and args.dry_run:
        p.error("--apply and --dry-run are mutually exclusive")
    path = os.path.join(args.data_dir, subjects.SUBJECTS_FILENAME) if args.data_dir else subjects.data_path()
    entries = builtin_entries()
    if os.path.exists(path):
        print(f"error: {path} already exists; refusing to overwrite it", file=sys.stderr)
        return 1
    print(f"{len(entries)} subjects ({sum(1 for e in entries if e['parent'])} with a parent) -> {path}")
    if not args.apply:
        print("dry run: nothing written. Re-run with --apply.")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subjects.save(entries, path)
    print(f"wrote {path}. Review and commit it in knowledge-private.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
