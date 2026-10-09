"""Byte-preserving backfill of keys into fact files, the rules (domain: no file, no lock).

Given the text of a JSON array file and the keys to add to each element, produce the new text with only
those keys inserted (matching each object's own layout) and verify that nothing else changed. The scripts
(`backfill_source_keys`, `backfill_dates`) read and write the files under the inter-process lock.
"""
import json

import revisions


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


def plan_additions(items, name, adders):
    """For each element of the parsed file `name`: the [(key, value)] pairs to insert. `adders` is a list of
    callables (entry, filename, index) -> {key: value}; keys the entry already has, or an earlier adder
    already supplied, are skipped."""
    additions = []
    for i, item in enumerate(items):
        pairs = []
        for adder in adders:
            for k, v in adder(item, name, i).items():
                if k not in item and k not in {p[0] for p in pairs}:
                    pairs.append((k, v))
        additions.append(pairs)
    return additions


def date_problems(name, items):
    """[(where, message)] for entries whose existing `date_added` is not an ISO date or timestamp (the
    backfill never overwrites an existing value)."""
    problems = []
    for i, item in enumerate(items):
        if "date_added" not in item:
            continue
        v = item["date_added"]
        try:
            revisions.parse_timestamp(v)
        except (TypeError, ValueError):
            problems.append((f"{name}[{i}]", f"date_added is {v!r}, not an ISO date or timestamp; fix it by hand "
                                             f"(the backfill never overwrites an existing value)"))
    return problems
