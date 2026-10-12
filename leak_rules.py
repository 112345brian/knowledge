"""Leak test rules (domain: no sqlite, no files).

How a private marker is matched (case-folded, NFC and NFD forms), which derived strings count as
markers, and how a finding is printed. `normal_db_service` gets the db's cells and the full DB's private
rows through the `LeakScan` port and applies these; `leak_scan_store` (the SQLite adapter) only reads.
"""
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, List, Set, Tuple

MIN_MARKER_LEN = 6  # shorter derived strings are too generic to be a meaningful marker


def variants(marker):
    out = set()
    for form in ("NFC", "NFD"):
        out.add(unicodedata.normalize(form, marker).lower())
    return out


def check_markers(markers):
    """`markers` as {label: text}; a bare iterable of strings labels each by itself. Raises ValueError on an
    empty marker (it would match everything)."""
    if not isinstance(markers, dict):
        markers = {m: m for m in markers}
    for label, marker in markers.items():
        if not marker or not marker.strip():
            raise ValueError(f"empty marker {label!r}: it would match everything")
    return markers


def leaks_in_bytes(raw_lower, markers):
    """[(label, marker, "raw file bytes")] for markers whose text occurs in the (already lowercased) file bytes."""
    return [(label, marker, "raw file bytes") for label, marker in markers.items()
            if any(v.encode("utf-8") in raw_lower for v in variants(marker))]


def leaks_in_cell(cell, where, markers):
    """[(label, marker, where)] for markers found in one text/blob cell (`where` is table.column)."""
    if isinstance(cell, bytes):
        cell = cell.decode("utf-8", "ignore")
    if not isinstance(cell, str):
        return []
    folded = unicodedata.normalize("NFC", cell).lower()
    return [(label, marker, where) for label, marker in markers.items()
            if any(unicodedata.normalize("NFC", v) in folded for v in variants(marker))]


def add_marker(markers, label, value, legit, shareable=False):
    """Register `value` as the marker `label` unless it is too short or (when `shareable`) text that a
    normal fact may legitimately also hold (e.g. the same quote from a shared source); claims, paths and
    subject names never are shareable."""
    if isinstance(value, str) and len(value.strip()) >= MIN_MARKER_LEN and not (shareable and value in legit):
        markers.setdefault(f"{label}: {value[:40]}", value)


def format_leaks(leaks):
    return "\n".join(f"  LEAK {label!r} found in {where}" for label, _m, where in leaks)


@dataclass(frozen=True)
class PrivateMaterial:
    """Everything in the full DB that must never reach the normal tier, as plain rows (what the `LeakScan` port
    returns; no connection crosses it). `legit` is every string the normal tier may hold legitimately."""
    legit: Set[str]
    private_facts: List[Tuple]               # (id, statement, notes, source_quote, trust_rationale)
    private_citations: List[Tuple]           # (fact_id, locator, quote) of private facts
    private_revisions: List[Tuple]           # (statement,) of non-normal revisions and of private facts' revisions
    claims: List[Tuple]                      # (id, statement, notes)
    vault_paths: List[Tuple]                 # (path,)
    origin_paths: List[Tuple]                # (origin_path,)
    build_commits: List[Tuple]               # (code_commit, private_commit)
    build_input_keys: List[Tuple]            # (input_key,)
    status_notes: List[Tuple]                # (status_note,)
    where_from: List[Tuple]                  # (where_from,)
    file_hashes: List[Tuple]                 # (hash,)
    private_subject_names: List[Tuple]       # (name,)
    private_entities: List[Tuple]            # (canonical_name, name_norm, entity_key, external_id, notes)
    private_entity_aliases: List[Tuple]      # (alias, alias_norm)
    private_subject_descriptions: List[Tuple]  # (description,)
    private_subject_aliases: List[Tuple]     # (alias,)


def markers_from(material):
    """{label: text} for everything in `material` that must never reach the normal tier."""
    markers = {}
    legit = material.legit

    def add(label, value, shareable=False):
        add_marker(markers, label, value, legit, shareable)

    for fid, st, notes, quote, rat in material.private_facts:
        add(f"private fact {fid} statement", st, True)
        add(f"private fact {fid} notes", notes, True)
        add(f"private fact {fid} source_quote", quote, True)
        add(f"private fact {fid} trust_rationale", rat, True)
    for fid, loc, quote in material.private_citations:
        add(f"private fact {fid} citation locator", loc, True)
        add(f"private fact {fid} citation quote", quote, True)
    for (st,) in material.private_revisions:
        add("private revision statement", st, True)
    for cid, st, notes in material.claims:
        add(f"claim {cid} statement", st)
        add(f"claim {cid} notes", notes)
    for (p,) in material.vault_paths:
        add("vault path", p)
    for (p,) in material.origin_paths:
        add("source origin_path", p)
    for (a, b) in material.build_commits:      # #47
        add("code commit", a)
        add("private commit", b)
    for (k,) in material.build_input_keys:
        add("build input key", k)
    for (n,) in material.status_notes:      # #41: a notice URL may be private; never copied
        add("source status note", n)
    for (u,) in material.where_from:   # #46: download URLs reveal interests
        add("where_from URL", u)
    for (h,) in material.file_hashes:   # #38 fixity: hashes identify private files
        add("file hash", h)
    for (n,) in material.private_subject_names:
        add("private subject name", n)
    # #42: a private entity's name, aliases, notes and id are as private as the facts that mention it
    for (n, norm, key, ext, notes) in material.private_entities:
        for label, value in (("private entity name", n), ("private entity name (normalized)", norm), ("private entity key", key),
                             ("private entity external id", ext), ("private entity notes", notes)):
            add(label, value)
    for (a, an) in material.private_entity_aliases:
        add("private entity alias", a)
        add("private entity alias (normalized)", an)
    # #43: a private subject's description and aliases are as private as its name
    for (d,) in material.private_subject_descriptions:
        add("private subject description", d)
    for (a,) in material.private_subject_aliases:
        add("private subject alias", a)
    return markers


def find_leaks(raw_bytes, cells, markers):
    """[(label, marker, where)]: `markers` (a dict or an iterable of strings; an empty one raises ValueError) found
    in the file's raw bytes, then in every (where, cell) of `cells`."""
    markers = check_markers(markers)
    leaks = leaks_in_bytes(raw_bytes.lower(), markers)
    for where, cell in cells:
        leaks.extend(leaks_in_cell(cell, where, markers))
    return leaks
