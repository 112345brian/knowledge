"""Review of pending facts (#6): list the queue, approve or reject items.

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
import os
from dataclasses import dataclass, field
from typing import List, Optional

import revisions
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

OUTCOMES = ("approved", "rejected", "skipped", "unknown", "error")


@dataclass
class ItemResult:
    ref: str
    outcome: str                      # one of OUTCOMES
    source_key: Optional[str] = None
    reason: Optional[str] = None      # why skipped / unknown / failed
    revision: Optional[dict] = None   # the appended revision for approved / rejected


@dataclass
class ReviewResult:
    items: List[ItemResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)   # batch-level failure (dirty tree, bad input)
    notes: List[str] = field(default_factory=list)
    commit: Optional[str] = None                      # short hash when a commit was made
    commit_error: Optional[str] = None                # revisions written but NOT committed
    detached: bool = False

    @property
    def changed(self):
        return [i for i in self.items if i.outcome in ("approved", "rejected")]

    @property
    def ok(self):
        """True when nothing went wrong: no batch error, failed commit, unknown ref or failed item.
        A skipped item (already reviewed) is not a failure."""
        return not self.errors and self.commit_error is None and all(
            i.outcome in ("approved", "rejected", "skipped") for i in self.items)


# --------------------------------------------------------------------------- reads

def list_pending(db):
    """Pending facts of a built db, oldest first (date_added, then id). `db` is a sqlite connection
    or a path (opened read-only). Each row: id, source_key, subject, statement, visibility,
    trust_level, captured_via, session_id, captured_at, source_quote, date_added."""
    with revisions._connection(db) as con:
        cur = con.execute(
            """SELECT f.id, f.source_key, s.name AS subject, f.statement, f.visibility, f.trust_level,
                      f.captured_via, f.session_id, f.captured_at, f.source_quote, f.date_added
               FROM facts f JOIN subjects s ON s.id = f.subject_id
               WHERE f.status = 'pending' ORDER BY f.date_added, f.id""")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def current_states(data_dir=None):
    """{source_key: current mutable snapshot incl. revision} from the entry files and revision
    log, in entry order (dict order). Raises revisions.RevisionError if either is unusable."""
    data_dir = revisions.default_data_dir() if data_dir is None else data_dir
    entries = revisions.load_entries(data_dir)
    states = {e["key"]: revisions.implicit_revision(e["key"], e["entry"], e["legacy_date"]) for e in entries}
    for _, rec in revisions.read_log(os.path.join(data_dir, revisions.REVISIONS_FILENAME)):
        if rec["source_key"] in states:
            states[rec["source_key"]] = rec
    return states


def _pending_keys(states, entries_order):
    return [k for k in entries_order if states[k]["status"] == "pending"]


# --------------------------------------------------------------------------- resolve

def _resolve(ref, states, db):
    """-> (source_key | None, problem | None)."""
    if isinstance(ref, bool) or not isinstance(ref, (int, str)):
        return None, "ref must be a fact id or a source_key"
    text = str(ref).strip()
    if not text:
        return None, "blank reference"
    if text.isdigit():
        if db is None:
            return None, "fact ids need a database to resolve; pass the source_key instead"
        with revisions._connection(db) as con:
            row = con.execute("SELECT source_key FROM facts WHERE id = ?", (int(text),)).fetchone()
        if row is None:
            return None, f"no fact with id {text}"
        if row[0] is None or row[0] not in states:
            return None, f"fact {text} has no source_key in the data files"
        return row[0], None
    if text not in states:
        return None, f"no fact with source_key {text!r}"
    return text, None


# --------------------------------------------------------------------------- transitions

def _transition(refs, to_status, outcome, reason, via, session_id, data_dir, commit, allow_dirty, db, verb):
    result = ReviewResult()
    if isinstance(refs, (str, int)):
        refs = [refs]
    refs = list(refs)
    if not isinstance(reason, str) or not reason.strip():
        result.errors.append("reason is required")
        return result
    data_dir = revisions.default_data_dir() if data_dir is None else data_dir
    log_path = os.path.join(data_dir, revisions.REVISIONS_FILENAME)
    try:
        states = current_states(data_dir)
    except revisions.RevisionError as e:
        result.errors.append(str(e))
        return result
    order = list(states)

    positioned = []

    def add(pos, item):
        positioned.append((pos, item))
        result.items = [i for _, i in sorted(positioned, key=lambda t: t[0])]  # input order, stable

    # Plan: one ItemResult per ref (or per pending fact for "all"), nothing written yet.
    plan, seen = [], set()
    for pos, ref in enumerate(refs):
        if isinstance(ref, str) and ref.strip().lower() == "all":
            todo = _pending_keys(states, order)
            for key in todo:
                if key not in seen:
                    seen.add(key)
                    plan.append((key, key, pos))
            if not todo:
                result.notes.append("no pending facts")
            continue
        key, problem = _resolve(ref, states, db)
        if problem:
            add(pos, ItemResult(str(ref), "error" if "database" in problem else "unknown", None, problem))
            continue
        if key in seen:
            add(pos, ItemResult(str(ref), "skipped", key, "listed more than once in this batch"))
            continue
        seen.add(key)
        plan.append((str(ref), key, pos))
    todo = []
    for ref, key, pos in plan:
        status = states[key]["status"]
        if status != "pending":
            add(pos, ItemResult(ref, "skipped", key, f"status is {status!r}, not pending"))
        else:
            todo.append((ref, key, pos))
    if not todo:
        return result

    repo = None
    if commit:
        try:
            repo = find_repo(data_dir)
            if repo is None:
                result.notes.append(f"{data_dir} is not inside a git repository; the change will not be committed.")
            elif not allow_dirty:
                ensure_clean_tree(repo)
        except PrivateGitError as e:
            result.errors.append(str(e))
            return result

    for ref, key, pos in todo:
        res = revisions.append_revision(key, {"status": to_status}, reason, via, session_id,
                                        data_dir=data_dir, expect={"status": "pending"})
        if res.ok:
            add(pos, ItemResult(ref, outcome, key, revision=res.revision))
        elif any(e.startswith("precondition failed") for e in res.errors):
            # Changed by someone else since we looked (or a concurrent identical call won).
            add(pos, ItemResult(ref, "skipped", key, "no longer pending: " + "; ".join(res.errors)))
        else:
            add(pos, ItemResult(ref, "error", key, "; ".join(res.errors)))

    if repo is not None and result.changed:
        message = f"{verb}: {len(result.changed)} fact(s) ({', '.join(i.source_key for i in result.changed[:5])}" \
                  f"{', ...' if len(result.changed) > 5 else ''})"
        try:
            result.commit = commit_private_change([log_path], message, repo)
            result.detached = is_detached(repo)
        except PrivateGitError as e:
            result.commit_error = f"the revisions ARE in {log_path} but are NOT committed: {e}"
    return result


def approve(refs, reason="approved", via="cli", session_id=None, data_dir=None, commit=True,
            allow_dirty=False, db=None):
    """Approve pending facts: append a status='active' revision for each. See the module docstring."""
    return _transition(refs, "active", "approved", reason, via, session_id, data_dir, commit, allow_dirty, db, "approve")


def reject(refs, reason, via="cli", session_id=None, data_dir=None, commit=True, allow_dirty=False, db=None):
    """Reject pending facts: append a status='retracted' revision for each. `reason` is required.
    Only pending facts are rejected; retracting an active fact is a different operation."""
    return _transition(refs, "retracted", "rejected", reason, via, session_id, data_dir, commit, allow_dirty, db, "reject")
