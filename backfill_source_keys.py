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
    python3 backfill_source_keys.py                       # dry run on the private data dir
    python3 backfill_source_keys.py --apply               # write
    python3 backfill_source_keys.py --data-dir DIR ...    # a copy, for trying it out

Extension point (used by backfill_dates.py, #35): pass `extra_adders` to `backfill()` -- each is
a callable (entry, filename, index) -> {key: value} of keys to insert for that entry; keys the
entry already has are skipped. `source_keys=False` turns the source_key pass off.
"""
import argparse
import json
import os
import sys

import revisions
from add_fact import _file_lock, _lock_path


def source_key_adder(keys_by_file):
    def add(entry, filename, index):
        return {"source_key": keys_by_file[filename][index]}
    return add


def _element_starts(text, path):
    """Offsets of each top-level array element: [(start, end)]."""
    dec = json.JSONDecoder()
    i = _skip_ws(text, 0)
    if i >= len(text) or text[i] != "[":
        raise revisions.RevisionError(f"{path} is not a JSON array")
    i += 1
    spans = []
    while True:
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == "]":
            return spans
        _, end = dec.raw_decode(text, i)
        spans.append((i, end))
        i = _skip_ws(text, end)
        if i < len(text) and text[i] == ",":
            i += 1


def _skip_ws(text, i):
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _insertion(text, start, pairs):
    """Text to insert right after the `{` at text[start], matching the object's own layout."""
    body = ", ".join(f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}" for k, v in pairs)
    after = text[start + 1:]
    j = _skip_ws(after, 0)
    if j < len(after) and after[j] == "}":  # empty object
        return f" {body} " if j else body
    ws = after[:j]
    if "\n" in ws:  # multi-line object: reuse the indent of its first key
        indent = ws.split("\n")[-1]
        return "".join(f"\n{indent}{json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}," for k, v in pairs)
    return f" {body}," if ws else f"{body}, "


def rewrite_text(text, path, additions):
    """`additions[i]` = list of (key, value) pairs to insert into element i. Returns
    (new_text, inserted_segments) with the insertions applied; verifies nothing else changed."""
    spans = _element_starts(text, path)
    segments = []  # (position, inserted text)
    for (start, _), pairs in zip(spans, additions):
        if pairs:
            segments.append((start + 1, _insertion(text, start, pairs)))
    new = text
    for pos, seg in reversed(segments):
        new = new[:pos] + seg + new[pos:]
    # Verify: stripping the insertions gives back the old bytes, and the parse differs only by the new keys.
    back, offset, actual = new, 0, []
    for pos, seg in segments:
        actual.append(pos + offset)
        offset += len(seg)
    for at, (_, seg) in reversed(list(zip(actual, segments))):
        if back[at:at + len(seg)] != seg:
            raise revisions.RevisionError(f"{path}: internal check failed (insertion not where expected)")
        back = back[:at] + back[at + len(seg):]
    if back != text:
        raise revisions.RevisionError(f"{path}: internal check failed (bytes outside the insertion changed)")
    old_items, new_items = json.loads(text), json.loads(new)
    for old, cur, pairs in zip(old_items, new_items, additions):
        added = {k for k, _ in pairs}
        if {k: v for k, v in cur.items() if k not in added} != old or any(cur[k] != v for k, v in pairs):
            raise revisions.RevisionError(f"{path}: internal check failed (parsed content differs beyond the added keys)")
    return new, segments


def _backfill_file(path, name, adders, apply):
    """Returns the number of entries changed in this file."""
    with _file_lock(_lock_path(path)):
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        items = revisions._read_array(path)
        keys = revisions.derive_keys(items, name)
        context = {name: keys}
        additions = []
        for i, item in enumerate(items):
            pairs = []
            for adder in adders(context):
                for k, v in adder(item, name, i).items():
                    if k not in item and k not in {p[0] for p in pairs}:
                        pairs.append((k, v))
            additions.append(pairs)
        changed = sum(1 for p in additions if p)
        if changed and apply:
            new, _ = rewrite_text(text, path, additions)
            revisions._atomic_write_text(path, new)
        elif changed:
            rewrite_text(text, path, additions)  # dry run still proves the rewrite would verify
    return changed


def backfill(data_dir, apply=False, extra_adders=(), source_keys=True):
    """Backfill source_key (when `source_keys`) and whatever `extra_adders` return across the
    legacy files. Returns {filename: entries_changed}. Nothing is written unless apply=True."""
    # All keys must be unique across files before we write anything.
    revisions.load_entries(data_dir)
    result = {}
    for name, _ in revisions.ENTRY_FILES:
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            continue
        result[name] = _backfill_file(path, name,
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
    data_dir = args.data_dir or revisions.default_data_dir()
    try:
        result = backfill(data_dir, apply=args.apply)
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
