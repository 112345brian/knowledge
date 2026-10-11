"""The fact entry and the revision record as types with behaviour.

Model ring: imports only other model modules. An entry (one JSON object in a data file) and a revision (one
line of the revision log) used to be dicts handled by free functions in `revisions`; the rules about what they
may contain, which fields are mutable, what a legacy entry means and what a snapshot is live here, on
`FactEntry` and `Revision`. `revisions` keeps its function names as thin delegations, so callers are unchanged.
Nothing here reads a file, the clock or the configuration.
"""
import json
from dataclasses import dataclass
from typing import Mapping, Optional

import validtime
from fact_rules import FRESHNESS_VALUES, KIND_VALUES, SOURCE_KEY_RE, VALID_TRUST, VALID_VISIBILITY, VIA_RE
from timestamps import has_offset

VALID_STATUS = ("pending", "active", "superseded", "retracted")
MUTABLE_FIELDS = ("statement", "trust_level", "trust_rationale", "status", "visibility",
                  "superseded_by", "recheck_by", "recheck_rationale", "freshness", "kind", "valid_from", "valid_to", "applies_to", "notes")
META_FIELDS = ("source_key", "revision", "changed_at", "changed_via", "session_id", "change_reason")
REVISION_KEYS = META_FIELDS + MUTABLE_FIELDS  # on-disk key order is part of the format
# (file, the date backfill_dates.py writes onto entries that have no date_added). The build no
# longer falls back to these (#35): 04/11 fail on an undated entry. Do not change them: they are
# the values the old LEGACY_DATE_ADDED constants had, i.e. the dates already in knowledge.db.
ENTRY_FILES = (
    [("pilot_facts.json", "2026-09-11")]
    + [(f"facts_batch{i}.json", "2026-09-11") for i in range(1, 5)]
    + [("general_facts.json", "2026-09-26")]
)


# #7. A stored fact's freshness is 'recheck', 'no-decay' or the legacy-only 'unreviewed' (keep in
# sync with the CHECK on facts.freshness in schema.sql).
UNREVIEWED = "unreviewed"
STORED_FRESHNESS = FRESHNESS_VALUES
LEGACY_FILES = tuple(name for name, _ in ENTRY_FILES if name != "general_facts.json")
UNREVIEWED_NOTE = ("freshness: unreviewed -- this fact predates the freshness field (#7) and has not been "
                   "individually reviewed; it has no recheck_by.")


class RevisionError(Exception):
    """The entry files or the revision log are not in a state we can safely use."""



def _present(value):
    return isinstance(value, str) and bool(value.strip())


def freshness_problem(freshness, recheck_by, recheck_rationale):
    """Why (freshness, recheck_by, recheck_rationale) is not a state the facts CHECKs allow, or
    None. A blank string counts as missing (the schema only sees NULL, plus the trim check on the
    rationale; we are stricter on recheck_by on purpose)."""
    if not isinstance(freshness, str) or freshness not in STORED_FRESHNESS:
        return f"freshness {freshness!r} must be one of {list(STORED_FRESHNESS)}"
    if freshness == "recheck" and not _present(recheck_by):
        return "freshness 'recheck' requires recheck_by"
    if freshness == "no-decay" and not _present(recheck_rationale):
        return "freshness 'no-decay' requires a non-blank recheck_rationale saying why the fact does not decay"
    return None


def clean_applies_to(value):
    """Normalize an applicability value (#45): None and blank/whitespace-only text become None, other text
    is stripped. Raises ValueError for a non-string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"applies_to must be text, not {type(value).__name__}")
    return value.strip() or None


@dataclass(frozen=True)
class FactEntry:
    """One fact entry of a data file, and the data file it came from (which decides whether it is legacy)."""
    data: Mapping
    file: Optional[str] = None

    def _label(self):
        return f"fact {(self.data.get('statement') or '')[:60]!r}"

    @property
    def is_legacy(self):
        """An entry that predates the freshness field: it sits in one of the original fact files and
        has no provenance (anything captured via add_fact/mcp/migrate carries `captured_via`)."""
        return self.file in LEGACY_FILES and not self.data.get("captured_via")

    def effective(self):
        """The entry as it enters the db. A legacy entry with no `freshness` becomes 'recheck' if it
        has a recheck_by, otherwise 'unreviewed' plus the 'predates this field' note appended to its
        notes; every other entry must already carry a valid freshness. Raises RevisionError (loudly,
        never a silent default). The returned mapping always has `freshness`."""
        entry, legacy = self.data, self.is_legacy
        fresh = entry.get("freshness")
        where = f"{self.file or 'entry'}: fact {(entry.get('statement') or '')[:60]!r}"
        if fresh is None:
            if not legacy:
                raise RevisionError(f"{where} has no `freshness` and is not a legacy entry; every new fact must carry "
                                    f"one of {[v for v in STORED_FRESHNESS if v != UNREVIEWED]}")
            eff = dict(entry)
            if _present(entry.get("recheck_by")):
                eff["freshness"] = "recheck"
            else:
                eff["freshness"] = UNREVIEWED
                eff["notes"] = (entry["notes"] + "\n" if entry.get("notes") else "") + UNREVIEWED_NOTE
            return eff
        if fresh == UNREVIEWED and not legacy:
            raise RevisionError(f"{where}: 'unreviewed' is legacy-only and not allowed here")
        problem = freshness_problem(fresh, entry.get("recheck_by"), entry.get("recheck_rationale"))
        if problem:
            raise RevisionError(f"{where}: {problem}")
        return entry

    @property
    def kind(self):
        """The entry's `kind` (#39); an entry without the key (every legacy entry) is 'unclassified'.
        Raises RevisionError for a present-but-invalid value (null and blank included): never a silent default."""
        if "kind" not in self.data:
            return "unclassified"
        kind = self.data["kind"]
        if not isinstance(kind, str) or kind not in KIND_VALUES:
            raise RevisionError(f"{self._label()}: kind {kind!r} must be one of {list(KIND_VALUES)}")
        return kind

    @property
    def validity(self):
        """(valid_from, valid_to) (#40); absent keys are None. Raises RevisionError for a value that is not a
        real YYYY / YYYY-MM / YYYY-MM-DD, or a valid_to before the valid_from (null and blank included)."""
        vf, vt = self.data.get("valid_from"), self.data.get("valid_to")
        bad = validtime.problems(vf, vt)
        if bad:
            raise RevisionError(f"{self._label()}: " + "; ".join(bad))
        return vf, vt

    @property
    def applies_to(self):
        """The entry's applies_to, normalized; RevisionError for a non-string."""
        try:
            return clean_applies_to(self.data.get("applies_to"))
        except ValueError as e:
            raise RevisionError(f"{self._label()}: {e}") from None

    def snapshot(self):
        """The mutable fields of the entry: what revision 1 says."""
        entry = self.data
        snap = {f: entry.get(f) for f in MUTABLE_FIELDS}
        snap["statement"] = (entry.get("statement") or "").strip()
        snap["status"] = entry.get("status") or "active"
        snap["visibility"] = entry.get("visibility") or "private"
        snap["kind"] = self.kind
        snap["valid_from"], snap["valid_to"] = self.validity
        snap["applies_to"] = self.applies_to
        return snap

    def first_revision(self, key, legacy_date):
        """Revision 1 as a full revision dict (what the build inserts into fact_revisions). `file` decides
        whether a missing freshness is legacy (see effective). Raises RevisionError for an entry the facts
        CHECK would reject."""
        eff = FactEntry(self.effective(), self.file)
        entry = eff.data
        rev = {"source_key": key, "revision": 1,
               "changed_at": entry.get("date_added") or legacy_date,
               "changed_via": entry.get("captured_via") or "original",
               "session_id": entry.get("session_id"),
               "change_reason": "original entry"}
        rev.update(eff.snapshot())
        return {k: rev[k] for k in REVISION_KEYS}


@dataclass(frozen=True)
class Revision:
    """One line of the revision log: a full snapshot of a fact's mutable fields plus who/when/why."""
    record: Mapping

    def shape_problems(self):
        """Problems with this record's own fields (not its relation to other lines)."""
        return _record_shape_problems(self.record)

    def log_line(self):
        return json.dumps({k: self.record[k] for k in REVISION_KEYS}, ensure_ascii=False)


def _record_shape_problems(rec):
    """Problems with one log record's own fields (not its relation to other lines)."""
    if not isinstance(rec, dict):
        return ["not a JSON object"]
    errs = []
    missing = [k for k in REVISION_KEYS if k not in rec]
    extra = [k for k in rec if k not in REVISION_KEYS]
    if missing:
        errs.append(f"missing key(s) {missing} (a revision is a full snapshot)")
    if extra:
        errs.append(f"unknown key(s) {extra}")
    if missing:
        return errs
    if not isinstance(rec["source_key"], str) or not SOURCE_KEY_RE.match(rec["source_key"]):
        errs.append(f"source_key {rec['source_key']!r} is not a valid key")
    if type(rec["revision"]) is not int or rec["revision"] < 2:
        errs.append(f"revision {rec['revision']!r} must be an integer >= 2 (revision 1 is the original entry)")
    if not has_offset(rec["changed_at"]):
        errs.append(f"changed_at {rec['changed_at']!r} must be an ISO-8601 timestamp with a UTC offset")
    if not isinstance(rec["changed_via"], str) or not VIA_RE.match(rec["changed_via"]):
        errs.append(f"changed_via {rec['changed_via']!r} must be a lowercase kebab-case token")
    if rec["session_id"] is not None and not (isinstance(rec["session_id"], str) and rec["session_id"].strip()):
        errs.append("session_id must be null or a non-empty string")
    if not isinstance(rec["change_reason"], str) or not rec["change_reason"].strip():
        errs.append("change_reason must be a non-empty string")
    if not isinstance(rec["statement"], str) or not rec["statement"].strip():
        errs.append("statement must be a non-empty string")
    if rec["trust_level"] not in VALID_TRUST:
        errs.append(f"trust_level {rec['trust_level']!r} must be one of {sorted(VALID_TRUST)}")
    if rec["status"] not in VALID_STATUS:
        errs.append(f"status {rec['status']!r} must be one of {sorted(VALID_STATUS)}")
    if rec["visibility"] not in VALID_VISIBILITY:
        errs.append(f"visibility {rec['visibility']!r} must be one of {sorted(VALID_VISIBILITY)}")
    if rec["superseded_by"] is not None and not (isinstance(rec["superseded_by"], str) and SOURCE_KEY_RE.match(rec["superseded_by"])):
        errs.append(f"superseded_by {rec['superseded_by']!r} must be null or a source_key")
    if rec["superseded_by"] is not None and rec["superseded_by"] == rec["source_key"]:
        errs.append("superseded_by points at the fact itself")
    errs.extend(validtime.problems(rec["valid_from"], rec["valid_to"]))
    if rec["applies_to"] is not None and not (isinstance(rec["applies_to"], str) and rec["applies_to"].strip()):
        errs.append("applies_to must be null or non-blank text (blank is normalized to null before it is written)")
    if rec["kind"] not in KIND_VALUES:
        errs.append(f"kind {rec['kind']!r} must be one of {list(KIND_VALUES)}")
    for k in ("trust_rationale", "recheck_by", "recheck_rationale", "freshness", "notes"):
        if rec[k] is not None and not isinstance(rec[k], str):
            errs.append(f"{k} must be null or a string")
    problem = freshness_problem(rec["freshness"], rec["recheck_by"], rec["recheck_rationale"])
    if problem:
        errs.append(problem + " (a revision is a full snapshot; the facts table would reject this combination)")
    return errs


