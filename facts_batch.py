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

import add_fact
import add_fact_store
import facts_batch_rules
import modes
import privacy
from facts_batch_rules import (BatchResult, DEFAULT_CAPTURED_VIA, DEFAULT_TRUST, ITEM_KEYS, ItemResult,  # noqa: F401  (the public API)
                               MAX_BATCH)
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

_norm = facts_batch_rules.norm
_parse_item = facts_batch_rules.parse_item


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
            fact = facts_batch_rules.build_fact(fields, requested_default, captured_via, session_id, status)
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
            existing = facts_batch_rules.existing_keys(add_fact_store.read_array(data_path))
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

    try:
        total, rejected = add_fact_store.append_records(data_path, entries, reject=facts_batch_rules.duplicate_reason)
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
