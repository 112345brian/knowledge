"""Batch add_facts (the facade: binds the use case in `facts_batch_service` to the real adapters and keeps the public API): the library behind `knowledge.py add-facts`, the MCP `add_facts` tool (#2) and
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
import add_fact
import add_fact_store
import modes  # noqa: F401  (kept importable as facts_batch.modes)
import facts_batch_service
import private_git
from facts_batch_rules import (BatchResult, DEFAULT_CAPTURED_VIA, DEFAULT_TRUST, ITEM_KEYS, ItemResult,  # noqa: F401  (the public API)
                               MAX_BATCH)
from ports import Ports, bind

PORTS = Ports(git=private_git, facts_file=add_fact_store, add_fact=add_fact, defaults=add_fact._Defaults())

add_facts = bind(facts_batch_service.add_facts, PORTS)
