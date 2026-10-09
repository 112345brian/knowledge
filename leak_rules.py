"""Leak test rules (domain: no sqlite, no files).

How a private marker is matched (case-folded, NFC and NFD forms), which derived strings count as
markers, and how a finding is printed. `leak_test` (the adapter) reads the db files and the full DB's
private rows and applies these.
"""
import unicodedata

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
