"""Batch add_facts: the library behind `knowledge.py add-facts`, the MCP `add_facts` tool (#2) and
the `register facts` review protocol (#32, docs/register-facts-protocol.md).

    result = facts_batch.add_facts([{"statement": "...", "subject": "coffee"}, ...])

One call = one validation pass, one lock, one atomic write of general_facts.json, one private_git
commit (#10). Reuses add_fact's validate_fact / build_entry / privacy resolver (#31) unchanged.

Decisions (each is tested):

  * Validation is ALL-OR-NOTHING. If any item is invalid (bad subject, unknown key, empty
    statement, a citekey the db does not have...) nothing is written; every item gets an outcome
    ('invalid' with its errors, or 'not_saved' for the valid ones) so the caller can fix the list
    and send it again whole.
  * Everything after validation is PER ITEM and never aborts the rest:
      - privacy: the requested visibility is only an INPUT; the stored value is the most
        restrictive of request, subject tag and keyword list (#31). A raise is not a refusal: the
        item is saved private and the result carries the rule that did it.
      - duplicates: an item whose (subject, statement) already exists in general_facts.json, or
        earlier in the same batch, is skipped with outcome 'duplicate' (so re-running a file
        writes nothing). Matching is exact apart from Unicode NFC, whitespace runs and case.
        It looks at every entry in the file, whatever its review status.
      - mode gate (only when a `session` is passed): a fact that resolves to private while the
        session is in normal mode is 'refused' (modes.WriteRefused text); the rest are saved.
        Mode off refuses the whole call.
  * Batch-level failures (not a list, over MAX_BATCH items, unreadable facts file, dirty
    knowledge-private tree, mode off) write nothing and set `result.errors`.
  * Facts reviewed in the chat are 'active' (a human reviewed them); pass status='pending' to queue
    them for review (#6).
  * Dates and statements are stored exactly as given (surrounding whitespace is trimmed by
    build_entry, nothing else). The tool never resolves 'next week' into a date.
  * Every item needs freshness (#7): `recheck_by`, or `no_decay: true` plus a non-blank
    `recheck_rationale` (neither, or both, makes the item invalid and, the batch being
    all-or-nothing, writes nothing). Freshness is derived; an item cannot supply it.
  * Caller-set source_key, status, captured_via, session_id and captured_at are not accepted per
    item: the batch assigns them. Unknown item keys are an error, never silently dropped.
  * Git (#10), same as add_fact: data dir inside a git repo -> clean tree required first (unless
    allow_dirty) and one commit of general_facts.json only; not in a repo -> written, with a note.
    A commit failure leaves the facts written and sets `commit_error` (CLI exit 3).
  * Visibility resolution sees the facts file as it was BEFORE the batch, so a new subject is
    private for every item of the batch that uses it (fail closed), not just the first.

Callers must read the result: tests/test_facts_batch.py scans the repo and fails on a discarded
`add_facts(...)` call.
"""
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import add_fact
import modes
import privacy
from add_fact import NewFact
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

MAX_BATCH = 1000
DEFAULT_TRUST = "unverified"   # a fact the user stated in chat: not independently verified
DEFAULT_CAPTURED_VIA = "register-facts"

_STR_KEYS = ("statement", "subject", "trust_level", "domain", "visibility", "kind", "valid_from", "valid_to", "trust_rationale", "notes",
             "recheck_by", "recheck_rationale", "source_citekey", "source_locator", "source_quote")
_BOOL_KEYS = ("is_original_claim", "is_personal", "no_decay")
ITEM_KEYS = frozenset(_STR_KEYS + _BOOL_KEYS)


@dataclass
class ItemResult:
    index: int                                  # position in the input list
    outcome: str                                # saved | would_save | invalid | not_saved | duplicate | refused
    statement: Optional[str] = None
    subject: Optional[str] = None
    source_key: Optional[str] = None            # set when saved / would_save (dry run: not stored)
    visibility: Optional[str] = None            # the RESOLVED value that is (or would be) stored
    requested: Optional[str] = None             # what the item/session asked for: an input only
    raised: bool = False                        # stored private although the request was not
    rule: Optional[str] = None                  # privacy.Resolution.explain(): the rule(s) behind `visibility`
    errors: List[str] = field(default_factory=list)  # why invalid / refused / duplicate / not_saved

    @property
    def stored(self):
        return self.outcome == "saved"


@dataclass
class BatchResult:
    items: List[ItemResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)   # batch-level: nothing was written
    notes: List[str] = field(default_factory=list)
    dry_run: bool = False
    total: Optional[int] = None                       # facts in general_facts.json afterwards
    commit: Optional[str] = None
    commit_error: Optional[str] = None                # facts written but NOT committed
    detached: bool = False

    @property
    def saved(self):
        return [i for i in self.items if i.outcome == "saved"]

    @property
    def ok(self):
        """True when nothing went wrong: no batch error, no failed commit, and every item saved,
        would-save or an exact duplicate of a fact already there."""
        return (not self.errors and self.commit_error is None
                and all(i.outcome in ("saved", "would_save", "duplicate") for i in self.items))

    def to_dict(self):
        d = asdict(self)
        d["ok"] = self.ok
        return d


def _norm(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


def _parse_item(raw):
    """(item_fields, errors). item_fields is None when the item is not usable at all."""
    if not isinstance(raw, dict):
        return None, [f"item must be an object, got {type(raw).__name__}"]
    errors = []
    unknown = sorted(str(k) for k in raw if k not in ITEM_KEYS)
    if unknown:
        errors.append(f"unknown key(s) {unknown}; allowed: {sorted(ITEM_KEYS)} (source_key, status and provenance are set by the batch)")
    for key in ("statement", "subject"):
        if raw.get(key) is None:
            errors.append(f"{key} is required")
    for key in _STR_KEYS:
        if raw.get(key) is not None and not isinstance(raw[key], str):
            errors.append(f"{key} must be a string")
    for key in _BOOL_KEYS:
        if raw.get(key) is not None and not isinstance(raw[key], bool):
            errors.append(f"{key} must be true or false")
    return raw, errors


def _default_requested(session):
    """The resolver input when an item names no visibility (modes.prepare_write's convention)."""
    if session is not None and modes.get_mode(session) == "private":
        return "private"
    return "normal"


def add_facts(items, status="active", captured_via=DEFAULT_CAPTURED_VIA, session_id=None, data_dir=None,
              dry_run=False, commit=True, allow_dirty=False, session=None, db_path=None):
    """Validate and append a batch of facts. Returns a BatchResult; never prints or exits.

    `items`: list of dicts with `statement` and `subject` required, plus freshness (#7: `recheck_by`, or
    `no_decay: true` with a `recheck_rationale`; neither or both is refused); optional trust_level (default
    'unverified'), notes, recheck_by, recheck_rationale, trust_rationale, source_quote,
    source_citekey, source_locator, domain, is_original_claim, is_personal, visibility (a request
    that can only raise privacy). `data_dir` defaults to PRIVATE_DATA_DIR. `session` (a modes
    Session) turns on the mode gate. `dry_run` validates and resolves but writes and commits nothing.
    """
    result = BatchResult(dry_run=bool(dry_run))
    data_path = add_fact.DATA_PATH if data_dir is None else os.path.join(data_dir, os.path.basename(add_fact.DATA_PATH))
    db_path = add_fact.DB_PATH if db_path is None else db_path

    if not isinstance(items, list):
        result.errors.append(f"items must be a list, got {type(items).__name__}")
        return result
    if len(items) > MAX_BATCH:
        result.errors.append(f"{len(items)} items is over the limit of {MAX_BATCH} per call; split the list")
        return result
    if not items:
        result.notes.append("no items; nothing to do")
        return result
    if session is not None and modes.get_mode(session) == "off":
        result.errors.append(str(modes.ModeOff("writing")))
        return result

    requested_default = _default_requested(session)
    facts = []  # NewFact | None, parallel to items
    for index, raw in enumerate(items):
        fields, errors = _parse_item(raw)
        item = ItemResult(index=index, outcome="invalid")
        result.items.append(item)
        if fields is not None:
            item.statement = fields.get("statement") if isinstance(fields.get("statement"), str) else None
            item.subject = fields.get("subject") if isinstance(fields.get("subject"), str) else None
        fact = None
        if not errors:
            fact = NewFact(
                statement=fields["statement"], subject=fields["subject"],
                trust_level=fields.get("trust_level") or DEFAULT_TRUST,
                no_decay=bool(fields.get("no_decay", False)),
                domain=fields.get("domain") or "general",
                is_original_claim=bool(fields.get("is_original_claim", False)),
                is_personal=bool(fields.get("is_personal", True)),
                visibility=fields.get("visibility") or requested_default,
                kind=fields.get("kind") or "unclassified",
                valid_from=fields.get("valid_from"), valid_to=fields.get("valid_to"),
                trust_rationale=fields.get("trust_rationale"), notes=fields.get("notes"),
                recheck_by=fields.get("recheck_by"), recheck_rationale=fields.get("recheck_rationale"),
                source_citekey=fields.get("source_citekey"), source_locator=fields.get("source_locator"),
                source_quote=fields.get("source_quote"),
                captured_via=captured_via, session_id=session_id, status=status)
            errors, notes = add_fact.validate_fact(fact, db_path)
            result.notes.extend(n for n in notes if n not in result.notes)
        item.errors = errors
        facts.append(fact if not errors else None)

    if any(i.errors for i in result.items):
        for i in result.items:
            if not i.errors:
                i.outcome = "not_saved"
                i.errors = ["not saved: another item in this batch is invalid (the batch is all-or-nothing)"]
        return result

    # Privacy: resolved for every item before anything is written.
    resolutions = []
    for item, fact in zip(result.items, facts):
        try:
            resolution = add_fact.resolve_privacy(fact, data_path, db_path)
        except (privacy.PrivacyRulesError, add_fact.DataFileError) as e:
            result.errors.append(str(e))
            for i in result.items:
                i.outcome, i.errors = "not_saved", [] if i is not item else [str(e)]
            return result
        resolutions.append(resolution)
        item.requested = fact.visibility
        item.visibility = resolution.visibility
        item.raised = resolution.raised_above_request
        item.rule = resolution.explain()

    todo = []  # (item, fact, resolution) cleared by the mode gate
    for item, fact, resolution in zip(result.items, facts, resolutions):
        if session is not None:
            try:
                stored = modes.check_write(session, resolution)
            except modes.WriteRefused as e:
                item.outcome, item.errors = "refused", [str(e)]
                continue
            assert stored == resolution.visibility
        todo.append((item, fact, resolution))
    if not todo:
        return result

    if dry_run:
        # Show what a real call would do, duplicates included (read-only look at the file).
        try:
            existing = {(e.get("subject"), _norm(e["statement"])) for e in add_fact._read_array(data_path)
                        if isinstance(e, dict) and isinstance(e.get("subject"), str) and isinstance(e.get("statement"), str)}
        except add_fact.DataFileError as e:
            result.errors.append(str(e))
            for item, _, _ in todo:
                item.outcome, item.errors = "not_saved", []
            return result
        for item, fact, _ in todo:
            key = (fact.subject, _norm(fact.statement))
            if key in existing:
                item.outcome, item.errors = "duplicate", ["already present (same subject and statement)"]
            else:
                existing.add(key)
                item.outcome = "would_save"
        return result

    repo = None
    if commit:
        try:
            repo = find_repo(os.path.dirname(data_path))
            if repo is None:
                result.notes.append(f"{os.path.dirname(data_path)} is not inside a git repository; the change will not be committed.")
            elif not allow_dirty:
                ensure_clean_tree(repo)
        except PrivateGitError as e:
            result.errors.append(str(e))
            for item, _, _ in todo:
                item.outcome, item.errors = "not_saved", []
            return result

    entries = [add_fact.build_entry(fact, visibility=resolution.visibility) for _, fact, resolution in todo]

    def reject(existing, entry):
        key = (entry["subject"], _norm(entry["statement"]))
        for e in existing:
            if isinstance(e, dict) and isinstance(e.get("statement"), str) and (e.get("subject"), _norm(e["statement"])) == key:
                return "already present (same subject and statement)"
        return None

    try:
        total, rejected = add_fact.append_records(data_path, entries, reject=reject)
    except add_fact.DataFileError as e:
        result.errors.append(str(e))
        for item, _, _ in todo:
            item.outcome, item.errors = "not_saved", []
        return result
    except OSError as e:
        result.errors.append(f"could not write {data_path}: {e}")
        for item, _, _ in todo:
            item.outcome, item.errors = "not_saved", []
        return result
    rejected = dict(rejected)
    for pos, ((item, _, _), entry) in enumerate(zip(todo, entries)):
        if pos in rejected:
            item.outcome, item.errors = "duplicate", [rejected[pos]]
        else:
            item.outcome, item.source_key = "saved", entry["source_key"]
    result.total = total

    saved = result.saved
    if repo is not None and saved:
        subjects = sorted({i.subject for i in saved})
        message = f"add-facts: {len(saved)} fact(s) ({', '.join(subjects[:3])}{', ...' if len(subjects) > 3 else ''})"
        try:
            result.commit = commit_private_change([data_path], message, repo)
            result.detached = is_detached(repo)
        except PrivateGitError as e:
            result.commit_error = f"the facts ARE in {data_path} but are NOT committed: {e}"
    return result
