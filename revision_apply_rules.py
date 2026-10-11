"""What applying the revision log to a built database means (pure; domain service).

Revision 1 is the entry as loaded; every later logged revision is a full snapshot. Applying the log inserts each
record into the history and then makes the facts row follow the LATEST revision of each fact: which fields it
carries, and how the `superseded_by` source key becomes a fact id. The store only writes what these functions
return; it decides nothing.
"""
from typing import Dict, Iterable, List, Mapping, Tuple

import revisions
from fact_entry import MUTABLE_FIELDS

# The fields of a fact row a revision sets: every mutable field, with `superseded_by` carried as a fact id.
STATE_FIELDS = tuple("superseded_by_fact_id" if f == "superseded_by" else f for f in MUTABLE_FIELDS)


def latest_by_key(records: Iterable[Tuple[int, dict]]) -> Dict[str, dict]:
    """{source_key: its last record in the log}, keys in order of first appearance."""
    latest: Dict[str, dict] = {}
    for _, rec in records:
        latest[rec["source_key"]] = rec
    return latest


def current_state(rec: Mapping, fact_ids: Mapping[str, int]) -> dict:
    """The values the facts row takes from revision `rec`; `superseded_by` is resolved to the replacement's fact id."""
    state = {f: rec[f] for f in MUTABLE_FIELDS if f != "superseded_by"}
    state["superseded_by_fact_id"] = fact_ids[rec["superseded_by"]] if rec["superseded_by"] else None
    return state


def originals_view(originals: Mapping[str, Mapping]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, dict]]:
    """(changed_at, freshness, legacy-inherited fields) by source key, from the revision-1 rows the store returns:
    the three things `validate_sequence` and the log parser need to know about each fact's original entry."""
    first = {k: v["changed_at"] for k, v in originals.items()}
    fresh = {k: v["freshness"] for k, v in originals.items()}
    base = {k: {f: v[f] for f in revisions.LEGACY_INHERITED} for k, v in originals.items()}
    return first, fresh, base


def validate(path: str, records: List[Tuple[int, dict]], fact_ids: Mapping[str, int], originals: Mapping[str, Mapping]) -> None:
    """Raise RevisionError (naming `path:line`) unless the log is a valid continuation of the originals."""
    first, fresh, _ = originals_view(originals)
    revisions.validate_sequence(path, records, first, set(fact_ids), fresh)
