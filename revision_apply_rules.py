"""What applying the revision log to a built database means (pure; domain service).

Revision 1 is the entry as loaded; every later logged revision is a full snapshot. Applying the log inserts each
record into the history and then makes the facts row follow the LATEST revision of each fact: which fields it
carries, and how the `superseded_by` source key becomes a fact id. The store only writes what these functions
return; it decides nothing.
"""
from typing import Dict, Iterable, List, Mapping, Tuple

import build_records as r
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


def write_back(fact_id: r.FactId, rec: Mapping, fact_ids: Mapping[str, r.FactId]) -> r.RevisionWriteBack:
    """What the facts row takes from revision `rec`; `superseded_by` is resolved to the replacement's fact id."""
    replacement = fact_ids[rec["superseded_by"]] if rec["superseded_by"] else None
    return r.RevisionWriteBack(fact_id, rec, replacement)


def inherited_by_key(firsts: Mapping[str, r.FirstRevision]) -> Dict[str, dict]:
    """{source_key: the legacy-inherited fields of its original entry}: what the log parser needs to fill a
    pre-#39 line that lacks them."""
    return {k: dict(v.inherited) for k, v in firsts.items()}


def validate(path: str, records: List[Tuple[int, dict]], fact_ids: Mapping[str, r.FactId],
             firsts: Mapping[str, r.FirstRevision]) -> None:
    """Raise RevisionError (naming `path:line`) unless the log is a valid continuation of the originals."""
    first = {k: v.changed_at for k, v in firsts.items()}
    fresh = {k: v.freshness for k, v in firsts.items()}
    revisions.validate_sequence(path, records, first, set(fact_ids), fresh)
