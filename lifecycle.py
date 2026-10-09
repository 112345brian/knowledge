"""Fact lifecycle (#8, #23): supersede, retract and set-visibility, each one appended revision.

This module is the facade (composition root): it binds the use case in `lifecycle_service` to the real
adapters and keeps the public API.

A fact entry in the JSON data files is never edited. A lifecycle change appends a revision (#30)
that carries the whole fact with the new status / visibility, so a rebuild shows it in `facts`,
`history` lists it with its reason, and `show --as-of` before it still shows the old state.

Library API (the Typer `supersede` / `retract` / `set-visibility` commands are thin callers, see
cli_lifecycle.py; the inbox and the MCP server will be too):
    supersede(ref, by_ref, reason, ...)          -> LifecycleResult   status 'superseded', superseded_by = replacement
    retract(ref, reason, ...)                    -> LifecycleResult   status 'retracted' (clears superseded_by)
    set_visibility(ref, visibility, reason, ...) -> LifecycleResult   'private' always; 'normal' only if the privacy floor allows
    edit_fact(ref, reason, statement=..., ...)   -> ReviseResult      one revision changing statement / trust / recheck / notes (#33)

`ref` / `by_ref` are a fact id (int, or a string of digits; needs `db`, a sqlite connection or a
path to the built db) or a source_key (works for facts from general_facts.json, pilot_facts.json and
facts_batch*.json alike). `reason` is required. Decisions are made from the data files + revision
log (the source of truth), not from the db, so they are right even when the db is stale; the db is
only used to turn a fact id into a source_key, and for the subject tree when lowering visibility.

Semantics (decided, tested):
  * One call = one fact = at most one revision. The precondition is repeated under the revision
    log's lock (append_revision's `expect`); if it fails because another process got there first,
    the call re-plans against the fresh state once, so a concurrent identical call is reported as
    "unchanged", never as a duplicate revision.
  * Outcomes: 'superseded' / 'retracted' / 'visibility_set' (a revision was written); 'unchanged'
    (already in the requested state: no revision, no tree check, no commit, ok); 'refused' (a rule
    said no; `reason` explains); 'unknown' (ref does not resolve); 'error' (anything else).
  * Retract: pending / active / superseded facts. Already retracted -> unchanged. Retracting also
    clears superseded_by (a retracted fact is not "replaced by" anything).
  * Supersede: only an ACTIVE fact (or a superseded one being re-pointed at another replacement).
    Refused: pending (approve or reject it first), retracted (reverse it with an `active` revision
    first), self-supersede, an unknown / retracted / pending replacement, and a cycle (A->B->A, or any
    longer chain that leads back to the fact). Same replacement again -> unchanged.
  * Set-visibility: raising to 'private' always works. Lowering to 'normal' is refused with the
    privacy resolver's own explanation when the floor says private (subject tag, ancestor tag,
    keyword/name rule, unknown subject): the build re-applies the floor raise-only, so a normal
    revision would be a silent no-op that misleads `history`. Lowering needs the built db for the
    subject tree and is refused without one (fail closed). Same visibility -> unchanged.
  * There is no un-supersede / un-retract command. Reversal is another revision, appended through
    the library: revisions_store.append_revision(key, {"status": "active", "superseded_by": None}, reason,
    via). The history keeps every step; nothing is deleted.
  * Git (#10): same as review.approve. If the data dir is in a git repo and commit=True, a dirty
    tree is refused up front (nothing written) unless allow_dirty; after the write ONE commit
    contains only the revision log file; a failed commit leaves the revision written and sets
    `commit_error` (CLI exit 3). Not in a repo: written, not committed, with a note.
Library code never prints or exits. Callers must check `.ok` (tests/test_lifecycle.py scans for it).
"""
import lifecycle_service
import lifecycle_store
import private_git
import privacy  # noqa: F401  (kept importable as lifecycle.privacy)
import review
import revisions  # noqa: F401  (kept importable as lifecycle.revisions)
import revisions_store
from lifecycle_rules import CHANGED_OUTCOMES, LifecycleResult, OUTCOMES, ReviseResult  # noqa: F401  (the public API)
from ports import Ports, bind

PORTS = Ports(git=private_git, revisions=revisions_store, reviews=review, lifecycle_reads=lifecycle_store)

supersede = bind(lifecycle_service.supersede, PORTS)
retract = bind(lifecycle_service.retract, PORTS)
set_visibility = bind(lifecycle_service.set_visibility, PORTS)
edit_fact = bind(lifecycle_service.edit_fact, PORTS)
