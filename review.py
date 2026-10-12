"""Review of pending facts (#6): list the queue, approve or reject items. The facade (composition root):
it binds the use case in `review_service` to the real adapters and keeps the public API.

New ad hoc facts are written with status 'pending' (add_fact.NewFact.status). Approving one
never edits its entry in the JSON data files; it appends a revision (#30) that carries the whole
fact with status 'active', so the history reads: revision 1 pending, revision 2 active. A rebuild
then shows the fact as active. Rejecting appends a 'retracted' revision the same way.

Library API (the Typer `review-pending` / `approve` commands and the inbox, #33, are thin callers):
    list_pending(db)                       -> [row dict, ...] oldest first (from the built db)
    approve(refs, reason=..., ...)         -> ReviewResult
    reject(refs, reason, ...)              -> ReviewResult

`refs` are fact ids (int, or a string of digits; needs `db`), source_keys, or "all" (every
pending fact). Decisions are made from the data files + revision log, the source of truth, not
from the db, so they are right even when the db has not been rebuilt since the last approval.

Batch semantics (decided, tested):
  * Items are independent. A refused or failing item is reported in its own ItemResult and does
    not stop the others; unknown refs are reported, not fatal.
  * Nothing is rolled back: the revision log is append-only, so earlier items of a batch stay
    written when a later one fails. The single commit covers every revision that was written.
  * Only a PENDING fact can be approved/rejected. Anything else (already active, retracted,
    superseded) is "skipped" with its current status, so approving twice changes nothing. The
    pending check is repeated under the revision log's lock (append_revision's `expect`), so a
    fact retracted by another process after we looked is skipped, never re-activated.
  * Git (#10): same semantics as add_fact. If the data dir is in a git repo and commit=True, a
    dirty tree is refused up front (nothing written) unless allow_dirty; after the writes ONE
    commit contains only fact_revisions.jsonl. Not in a repo: written, not committed, with a note.
    A batch with nothing to do neither checks the tree nor commits.
Library code never prints or exits.
"""
import private_git
import privacy_store
import review_service
import review_store
import revisions_store
from wiring import bind_all

API = ("list_pending", "current_states", "default_data_dir", "rules_with_db_context", "pending_view", "resolve_ref",
       "approve", "reject")


def compose(locations, clock):
    """The review API wired to the real adapters for these `locations`."""
    deps = review_service.ReviewDeps(git=private_git, revisions=revisions_store, rules=privacy_store,
                                     reads=review_store, clock=clock, locations=locations)
    return bind_all(review_service, deps, API)
