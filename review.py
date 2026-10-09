"""Review of pending facts (#6): list the queue, approve or reject items. The use case: it orchestrates
the adapters (`review_store`, `revisions_store`, `privacy_store`, `private_git`) and applies the rules
in `review_rules` (domain).

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

import privacy
import privacy_store
import revisions
import revisions_store
import review_rules
import review_store
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached
from review_rules import ItemResult, OUTCOMES, ReviewResult, status_word, visibility_basis  # noqa: F401  (the public API)

list_pending = review_store.list_pending
current_states = review_store.current_states
default_data_dir = review_store.default_data_dir

TRUST_LEVELS = revisions.VALID_TRUST


# --------------------------------------------------------------------------- display (the inbox page)

def rules_with_db_context(data_dir, db):
    """The privacy rules of `data_dir` plus the subject tree from the db (read-only), as
    `privacy check` does. A missing or odd db only means less context, never a crash.
    Raises privacy.PrivacyRulesError when the rules file is unusable."""
    rules = privacy_store.load_rules(privacy_store.rules_path(data_dir))
    if db is None:
        return rules
    try:
        parents = review_store.subject_parents(db)
    except Exception:  # noqa: BLE001
        return rules
    return rules.with_context(parents=parents, known_subjects=set(parents))


def pending_view(db_path, data_dir):
    """Everything a review page shows: {facts, hidden_reviewed, missing_from_snapshot, error}.
    Pending rows come from the built db; each is overlaid with the current state from the data
    files and revision log, and dropped when that says it is no longer pending. Never raises."""
    vm = {"facts": [], "hidden_reviewed": 0, "missing_from_snapshot": 0, "error": None}
    if not os.path.exists(db_path):
        vm["error"] = f"{db_path} does not exist: run `knowledge.py build` first"
        return vm
    try:
        rows = list_pending(db_path)
        states = current_states(data_dir)
        rules = rules_with_db_context(data_dir, db_path)
    except (revisions.RevisionError, privacy.PrivacyRulesError) as e:
        vm["error"] = str(e)
        return vm
    except Exception as e:  # noqa: BLE001 - e.g. sqlite3.Error on a half-built db: show it, don't crash
        vm["error"] = f"could not read the database: {e}"
        return vm
    listed = set()
    for r in rows:
        key = r["source_key"]
        listed.add(key)
        cur = states.get(key)
        if cur is not None and cur["status"] != "pending":
            vm["hidden_reviewed"] += 1
            continue
        vm["facts"].append(review_rules.overlay_pending(r, cur, rules))
    vm["missing_from_snapshot"] = sum(1 for k, s in states.items() if s["status"] == "pending" and k not in listed)
    return vm


# --------------------------------------------------------------------------- resolve

def resolve_ref(ref, states, db):
    """-> (source_key | None, problem | None). A fact id needs `db` (a connection or a path);
    a source_key resolves from `states` alone. May raise sqlite3.Error when the db is unreadable."""
    if isinstance(ref, bool) or not isinstance(ref, (int, str)):
        return None, "ref must be a fact id or a source_key"
    text = str(ref).strip()
    if not text:
        return None, "blank reference"
    if text.isdigit():
        if db is None:
            return None, "fact ids need a database to resolve; pass the source_key instead"
        with revisions_store.connection(db) as con:
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
    data_dir = revisions_store.default_data_dir() if data_dir is None else data_dir
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
            todo = review_rules.pending_keys(states, order)
            for key in todo:
                if key not in seen:
                    seen.add(key)
                    plan.append((key, key, pos))
            if not todo:
                result.notes.append("no pending facts")
            continue
        key, problem = resolve_ref(ref, states, db)
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
        res = revisions_store.append_revision(key, {"status": to_status}, reason, via, session_id,
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
