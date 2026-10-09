"""Fact revisions (#30): an append-only log of changes to facts, queryable as history.

A fact entry in the JSON data files is never edited after it is written. The entry itself is
the implicit revision 1 (its original state). Every later change appends one line to
`fact_revisions.jsonl` in the private data dir: a full snapshot of the mutable fields plus
{source_key, revision, changed_at, changed_via, session_id, change_reason}. The build
(`12_apply_fact_revisions.py`) validates the log, fills the `fact_revisions` table and writes
each fact's latest revision into the `facts` row.

This module is the DOMAIN: the rules for entries, keys, freshness, the log format and the next
revision, as pure functions over plain data (import-linter contract "domain-has-no-infrastructure").
Everything that touches the world lives in the adapter `revisions_store` (the entry files, the log
file and its lock, the clock, the db):
    revisions_store.append_revision(source_key, changes, reason, via, session_id=None)  -> RevisionResult
    revisions_store.get_history(db, fact_id | source_key)                               -> [revision dict, ...]
    revisions_store.get_fact_as_of(db, fact_id | source_key, "YYYY-MM-DD")              -> revision dict | None

`source_key` is the stable identity of a fact. New facts get one from add_fact.py. Entries
that predate it get a deterministic one derived from their content (`derive_keys`), the
same value `backfill_source_keys.py` later writes into the file, so building before or
after the backfill gives identical keys.

Nothing here reads a file, the clock or paths.py; the adapter passes everything in.
"""
import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Optional

from fact_rules import FRESHNESS_VALUES, SOURCE_KEY_RE, VALID_TRUST, VALID_VISIBILITY, VIA_RE
from timestamps import has_offset, parse_as_of, parse_timestamp  # noqa: F401  (re-exported: callers use revisions.parse_timestamp)

REVISIONS_FILENAME = "fact_revisions.jsonl"
VALID_STATUS = ("pending", "active", "superseded", "retracted")
MUTABLE_FIELDS = ("statement", "trust_level", "trust_rationale", "status", "visibility",
                  "superseded_by", "recheck_by", "recheck_rationale", "freshness", "notes")
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


# --------------------------------------------------------------------------- keys

def derive_keys(entries, filename):
    """One source_key per entry. An explicit `source_key` is kept as is. Others get
    `legacy-<10 hex of sha1(filename, subject, statement)>`, with `-2`, `-3`, ... for the
    2nd, 3rd entry of the same file with identical content (file order). Deterministic and
    independent of any other entry's key, so a partly backfilled file keeps its keys."""
    taken = {e["source_key"] for e in entries if isinstance(e, dict) and isinstance(e.get("source_key"), str)}
    seen = {}
    keys = []
    for e in entries:
        explicit = e.get("source_key") if isinstance(e, dict) else None
        if explicit is not None:
            keys.append(explicit)
            continue
        subject = ((e.get("subject") or "") if isinstance(e, dict) else "").strip()
        statement = ((e.get("statement") or "") if isinstance(e, dict) else "").strip()
        digest = hashlib.sha1("\n".join((filename, subject, statement)).encode()).hexdigest()[:10]
        n = seen.get(digest, 0) + 1
        seen[digest] = n
        key = f"legacy-{digest}" + (f"-{n}" if n > 1 else "")
        if key in taken:
            raise RevisionError(f"{filename}: derived source_key {key!r} collides with an explicit one")
        keys.append(key)
    return keys


# --------------------------------------------------------------------------- entries

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


def is_legacy_entry(entry, file):
    """An entry that predates the freshness field: it sits in one of the original fact files and
    has no provenance (anything captured via add_fact/mcp/migrate carries `captured_via`)."""
    return file in LEGACY_FILES and not entry.get("captured_via")


def effective_entry(entry, file=None):
    """The entry as it enters the db. A legacy entry with no `freshness` becomes 'recheck' if it
    has a recheck_by, otherwise 'unreviewed' plus the 'predates this field' note appended to its
    notes; every other entry must already carry a valid freshness. Raises RevisionError (loudly,
    never a silent default). The returned dict always has `freshness`."""
    legacy = is_legacy_entry(entry, file)
    fresh = entry.get("freshness")
    where = f"{file or 'entry'}: fact {(entry.get('statement') or '')[:60]!r}"
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


def entry_snapshot(entry):
    """The mutable fields of a JSON entry: what revision 1 says."""
    snap = {f: entry.get(f) for f in MUTABLE_FIELDS}
    snap["statement"] = (entry.get("statement") or "").strip()
    snap["status"] = entry.get("status") or "active"
    snap["visibility"] = entry.get("visibility") or "private"
    return snap


def implicit_revision(key, entry, legacy_date, file=None):
    """Revision 1 as a full revision dict (what 04/11 insert into fact_revisions). `file` is the
    entry's data file; it decides whether a missing freshness is legacy (see effective_entry).
    Raises RevisionError for an entry the facts CHECK would reject."""
    entry = effective_entry(entry, file)
    rev = {"source_key": key, "revision": 1,
           "changed_at": entry.get("date_added") or legacy_date,
           "changed_via": entry.get("captured_via") or "original",
           "session_id": entry.get("session_id"),
           "change_reason": "original entry"}
    rev.update(entry_snapshot(entry))
    return {k: rev[k] for k in REVISION_KEYS}


# --------------------------------------------------------------------------- log format

def validate_record_shape(rec):
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
    for k in ("trust_rationale", "recheck_by", "recheck_rationale", "freshness", "notes"):
        if rec[k] is not None and not isinstance(rec[k], str):
            errs.append(f"{k} must be null or a string")
    problem = freshness_problem(rec["freshness"], rec["recheck_by"], rec["recheck_rationale"])
    if problem:
        errs.append(problem + " (a revision is a full snapshot; the facts table would reject this combination)")
    return errs


def validate_sequence(path, records, first_revisions, known_keys, first_freshness=None):
    """Cross-line rules. `first_revisions` maps source_key -> revision-1 changed_at;
    `known_keys` is every existing source_key; `first_freshness` (optional) maps source_key ->
    revision-1 freshness, so a revision cannot go back to 'unreviewed' once a fact has been
    reviewed (it is a legacy marker, not a value a revision may choose). Raises RevisionError
    naming path:line."""
    last = {}
    fresh = dict(first_freshness or {})
    for k, v in first_revisions.items():
        try:
            last[k] = (1, parse_timestamp(v))
        except (TypeError, ValueError) as e:
            raise RevisionError(f"fact {k!r}: its original date_added {v!r} is not a date or timestamp") from e
    for n, rec in records:
        key = rec["source_key"]
        where = f"{path}:{n}"
        if key not in known_keys:
            raise RevisionError(f"{where}: unknown source_key {key!r} (no such fact)")
        prev_rev, prev_at = last[key]
        if rec["revision"] != prev_rev + 1:
            raise RevisionError(f"{where}: revision {rec['revision']} for {key!r} but expected {prev_rev + 1} "
                                f"(revisions must be contiguous from 2; this is a gap or a duplicate)")
        at = parse_timestamp(rec["changed_at"])
        if at < prev_at:
            raise RevisionError(f"{where}: changed_at {rec['changed_at']} for {key!r} is earlier than the previous "
                                f"revision ({prev_at.isoformat()}); timestamps must not go backwards")
        if rec["superseded_by"] is not None and rec["superseded_by"] not in known_keys:
            raise RevisionError(f"{where}: superseded_by {rec['superseded_by']!r} is not an existing source_key")
        if first_freshness is not None and rec["freshness"] == UNREVIEWED and fresh.get(key) != UNREVIEWED:
            raise RevisionError(f"{where}: freshness 'unreviewed' cannot be chosen by a revision; {key!r} was already reviewed")
        fresh[key] = rec["freshness"]
        last[key] = (rec["revision"], at)


# --------------------------------------------------------------------------- append

@dataclass
class RevisionResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    revision: Optional[dict] = None


# --------------------------------------------------------------------------- entries from files

def collect_entries(files):
    """Every fact entry, in file order, as dicts {file, index, key, entry, legacy_date}.
    `files` is [(name, legacy_date, items)] for the files that exist (items = the parsed JSON array).
    A duplicate or malformed key fails."""
    out, seen = [], {}
    for name, legacy_date, items in files:
        for i, (item, key) in enumerate(zip(items, derive_keys(items, name))):
            if not isinstance(key, str) or not SOURCE_KEY_RE.match(key):
                raise RevisionError(f"{name}[{i}]: source_key {key!r} must match {SOURCE_KEY_RE.pattern}")
            if key in seen:
                raise RevisionError(f"duplicate source_key {key!r} in {name}[{i}] and {seen[key]}")
            seen[key] = f"{name}[{i}]"
            out.append({"file": name, "index": i, "key": key, "entry": item, "legacy_date": legacy_date})
    return out


def check_array(items, path):
    """`items` (a parsed entry file) if it is a JSON array of objects, else RevisionError."""
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise RevisionError(f"{path} must be a JSON array of objects")
    return items


# --------------------------------------------------------------------------- log text

def parse_log(lines, path):
    """Parse the log: [(line_number, record)] from an iterable of text lines. Blank lines are
    skipped. A line that is not a valid record raises RevisionError naming `path:line`."""
    out = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise RevisionError(f"{path}:{n}: not valid JSON ({e})") from e
        errs = validate_record_shape(rec)
        if errs:
            raise RevisionError(f"{path}:{n}: " + "; ".join(errs))
        out.append((n, rec))
    return out


def log_line(rev):
    return json.dumps({k: rev[k] for k in REVISION_KEYS}, ensure_ascii=False)


# --------------------------------------------------------------------------- next revision

def check_request(changes, reason, via, session_id, expect):
    """Problems with the arguments of an append, before anything is read (a list, empty when fine)."""
    errors = []
    if not isinstance(changes, dict) or not changes:
        return ["changes must be a non-empty dict of mutable fields"]
    unknown = [k for k in changes if k not in MUTABLE_FIELDS]
    if unknown:
        errors.append(f"cannot change {unknown}; mutable fields are {list(MUTABLE_FIELDS)}")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("reason is required")
    if not isinstance(via, str) or not VIA_RE.match(via):
        errors.append(f"via {via!r} must be a lowercase kebab-case token (e.g. 'cli', 'mcp', 'approve')")
    if session_id is not None and not (isinstance(session_id, str) and session_id.strip()):
        errors.append("session_id must be None or a non-empty string")
    if expect is not None and (not isinstance(expect, dict) or any(k not in MUTABLE_FIELDS for k in expect)):
        errors.append(f"expect must be a dict over the mutable fields {list(MUTABLE_FIELDS)}")
    return errors


def plan_revision(source_key, changes, reason, via, session_id, expect, entries, records, now_iso, revisions_path):
    """The next revision of `source_key` given the loaded `entries` (collect_entries) and log
    `records` (parse_log), or the reasons it cannot be written. Pure: the caller holds the lock,
    supplies the time and writes the line. Returns RevisionResult; may raise RevisionError when the
    existing log is inconsistent."""
    by_key = {e["key"]: e for e in entries}
    if source_key not in by_key:
        return RevisionResult(False, [f"unknown source_key {source_key!r}"])
    first = {e["key"]: e["entry"].get("date_added") or e["legacy_date"] for e in entries}
    validate_sequence(revisions_path, records, first, set(by_key),
                      {e["key"]: implicit_revision(e["key"], e["entry"], e["legacy_date"], e["file"])["freshness"]
                       for e in entries})
    mine = [r for _, r in records if r["source_key"] == source_key]
    current = mine[-1] if mine else implicit_revision(source_key, by_key[source_key]["entry"],
                                                        by_key[source_key]["legacy_date"], by_key[source_key]["file"])
    if expect:
        wrong = {k: current[k] for k, v in expect.items() if current[k] != v}
        if wrong:
            return RevisionResult(False, [f"precondition failed: current {wrong}, expected "
                                          f"{ {k: expect[k] for k in wrong} }"])
    snapshot = {f: current[f] for f in MUTABLE_FIELDS}
    snapshot.update(changes)
    if isinstance(snapshot["statement"], str):
        snapshot["statement"] = snapshot["statement"].strip()
    if all(snapshot[f] == current[f] for f in MUTABLE_FIELDS):
        return RevisionResult(False, ["no field would change"])
    rev = {"source_key": source_key, "revision": current["revision"] + 1, "changed_at": now_iso,
           "changed_via": via, "session_id": session_id, "change_reason": reason.strip(), **snapshot}
    rev = {k: rev[k] for k in REVISION_KEYS}
    errs = validate_record_shape(rev)
    if rev["freshness"] == UNREVIEWED and current["freshness"] != UNREVIEWED:
        errs.append("freshness 'unreviewed' cannot be chosen by a revision; choose recheck or no-decay")
    if not errs and rev["superseded_by"] is not None and rev["superseded_by"] not in by_key:
        errs.append(f"superseded_by {rev['superseded_by']!r} is not an existing source_key")
    if not errs and parse_timestamp(rev["changed_at"]) < parse_timestamp(current["changed_at"]):
        errs.append(f"clock went backwards: now {rev['changed_at']} is before the previous revision "
                    f"({current['changed_at']}); refusing to write a line the build would reject")
    if errs:
        return RevisionResult(False, errs)
    return RevisionResult(True, revision=rev)


def revision_as_of(rows, as_of):
    """The latest of `rows` (revisions of one fact) whose changed_at is <= `as_of`, or None if the
    fact did not exist yet. Raises ValueError on a bad date."""
    cutoff = parse_as_of(as_of)
    chosen = None
    for r in rows:
        if parse_timestamp(r["changed_at"]) <= cutoff:
            chosen = r
    return chosen
