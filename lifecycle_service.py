"""Fact lifecycle (#8, #23): supersede, retract and set-visibility, each one appended revision.

This module is the use case: it loads the state through the ports (`ports`), asks `lifecycle_rules`
(domain) what to do, writes the revision and commits. It imports no adapter; `lifecycle` is the facade
that binds it to the real ones and keeps the public API.

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
import os

import lifecycle_rules
import privacy
import review_rules
import revisions
from lifecycle_rules import (CHANGED_OUTCOMES, LifecycleResult, OUTCOMES, ReviseResult,  # noqa: F401  (re-exported by the facade)
                             fail as _fail, gated as _gated, validate_edit as _validate_edit)

_decide_retract = lifecycle_rules.decide_retract
_decide_supersede = lifecycle_rules.decide_supersede
_decide_visibility = lifecycle_rules.decide_visibility


# --------------------------------------------------------------------------- engine

def _run(ports, verb, ref, by_ref, reason, via, session_id, data_dir, commit, allow_dirty, db, decide, ctx, message,
         expect_status=None):
    result = LifecycleResult(verb, str(ref))
    if not isinstance(reason, str) or not reason.strip():
        result.errors.append("reason is required")
        return result
    data_dir = ports.revisions.default_data_dir() if data_dir is None else data_dir
    log_path = os.path.join(data_dir, revisions.REVISIONS_FILENAME)
    try:
        states = ports.reviews.current_states(data_dir)
        entries = {e["key"]: e for e in ports.revisions.load_entries(data_dir)}
    except revisions.RevisionError as e:
        result.errors.append(str(e))
        return result
    key, problem = ports.reviews.resolve_ref(ref, states, db)
    if problem:
        result.outcome = "error" if "database" in problem else "unknown"
        result.reason = problem
        return result
    result.source_key = key
    ctx = dict(ctx, db=db, data_dir=data_dir, entries=entries,
               floor_inputs=lambda: ports.lifecycle_reads.floor_inputs(data_dir, db))
    if by_ref is not None:
        by_key, problem = ports.reviews.resolve_ref(by_ref, states, db)
        if problem:
            result.outcome = "error" if "database" in problem else "unknown"
            result.reason = "replacement: " + problem
            return result
        ctx["by_key"] = by_key
        if states[by_key]["status"] == "superseded":
            result.notes.append(f"the replacement {by_key!r} is itself superseded")

    plan = _gated(decide, states, key, ctx, expect_status)
    if plan[0] != "write":
        result.outcome, result.reason = plan[0], plan[1]
        return result

    repo = None
    if commit:
        try:
            repo = ports.git.find_repo(data_dir)
            if repo is None:
                result.notes.append(f"{data_dir} is not inside a git repository; the change will not be committed.")
            elif not allow_dirty:
                ports.git.ensure_clean_tree(repo)
        except ports.git.PrivateGitError as e:
            result.errors.append(str(e))
            return result

    for attempt in (1, 2):
        _, changes, expect, outcome = plan
        res = ports.revisions.append_revision(key, changes, reason, via, session_id, data_dir=data_dir, expect=expect)
        if res.ok:
            result.outcome, result.revision = outcome, res.revision
            break
        if attempt == 1 and any(e.startswith("precondition failed") for e in res.errors):
            # Someone else changed the fact since we looked: decide again on the fresh state.
            try:
                states = ports.reviews.current_states(data_dir)
            except revisions.RevisionError as e:
                result.errors.append(str(e))
                return result
            plan = _gated(decide, states, key, ctx, expect_status)
            if plan[0] != "write":
                result.outcome, result.reason = plan[0], "changed by another process: " + plan[1]
                return result
            continue
        result.outcome = "error"
        result.reason = "; ".join(res.errors)
        return result

    if repo is not None:
        try:
            result.commit = ports.git.commit_private_change([log_path], message(key, reason), repo)
            result.detached = ports.git.is_detached(repo)
        except ports.git.PrivateGitError as e:
            result.commit_error = f"the revision IS in {log_path} but is NOT committed: {e}"
    return result


# --------------------------------------------------------------------------- public API

def supersede(ports, ref, by_ref, reason, via="cli", session_id=None, data_dir=None, commit=True,
              allow_dirty=False, db=None):
    """Mark `ref` superseded by `by_ref` (a fact id or source_key). `reason` is required."""
    return _run(ports, "supersede", ref, by_ref, reason, via, session_id, data_dir, commit, allow_dirty, db,
                _decide_supersede, {}, lambda key, _r: f"supersede: {key}")


def retract(ports, ref, reason, via="cli", session_id=None, data_dir=None, commit=True, allow_dirty=False, db=None):
    """Mark `ref` retracted. `reason` is required."""
    return _run(ports, "retract", ref, None, reason, via, session_id, data_dir, commit, allow_dirty, db,
                _decide_retract, {}, lambda key, _r: f"retract: {key}")


def set_visibility(ports, ref, visibility, reason, via="cli", session_id=None, data_dir=None, commit=True,
                   allow_dirty=False, db=None, expect_status=None, raise_only=False):
    """Set `ref`'s visibility to 'normal' or 'private'. Lowering to normal needs `db` and is refused
    when the privacy rules say private. `reason` is required.
    `expect_status` (the inbox passes 'pending'): a fact not in that status is 'unchanged', and the
    status is part of the write's precondition. `raise_only`: any request for 'normal' is refused
    (naming the privacy rule when one floors the fact); only raising to 'private' is allowed."""
    if visibility not in ("normal", "private"):
        return LifecycleResult("set-visibility", str(ref), errors=[f"visibility {visibility!r} must be 'normal' or 'private'"])
    return _run(ports, "set-visibility", ref, None, reason, via, session_id, data_dir, commit, allow_dirty, db,
                _decide_visibility, {"visibility": visibility, "raise_only": raise_only},
                lambda key, _r: f"set-visibility: {key} {visibility}", expect_status)


# --------------------------------------------------------------------------- edit (#33)
# Moved from inbox.py so the serving layer holds no write logic; behavior unchanged. `edit` (the
# CLI) and the inbox page both call edit_fact.

def _revise(ports, ref, changes, reason, via, session_id, data_dir, commit, allow_dirty, db, expect_status, verb, adjust=None):
    """Resolve, check, one locked write, one commit.
    `adjust(key, current, changes) -> (changes, ReviseResult | None)` may add fields or refuse."""
    ref = str(ref)
    if not isinstance(reason, str) or not reason.strip():
        return _fail(ref, "reason is required")
    data_dir = ports.revisions.default_data_dir() if data_dir is None else data_dir
    try:
        states = ports.reviews.current_states(data_dir)
    except revisions.RevisionError as e:
        return _fail(ref, str(e))
    try:
        key, problem = ports.reviews.resolve_ref(ref, states, db)
    except ports.lifecycle_reads.DB_ERRORS as e:
        return _fail(ref, f"could not read the database to resolve fact id {ref}: {e}")
    if problem:
        return _fail(ref, problem, "error")
    current = states[key]
    if expect_status is not None and current["status"] != expect_status:
        return ReviseResult(True, "skipped", ref, key, f"already {review_rules.status_word(current['status'])}: nothing changed")
    if current["status"] == "retracted":
        return ReviseResult(False, "skipped", ref, key, "already retracted: not edited")
    notes = []
    if adjust is not None:
        try:
            changes, early = adjust(key, current, dict(changes))
        except privacy.PrivacyRulesError as e:
            return _fail(ref, str(e), source_key=key)
        if early is not None:
            early.ref, early.source_key = ref, key
            return early
    if all(current[k] == v for k, v in changes.items()):
        return _fail(ref, "no field would change", source_key=key)

    repo, log_path = None, os.path.join(data_dir, revisions.REVISIONS_FILENAME)
    if commit:
        try:
            repo = ports.git.find_repo(data_dir)
            if repo is None:
                notes.append(f"{data_dir} is not inside a git repository; the change will not be committed.")
            elif not allow_dirty:
                ports.git.ensure_clean_tree(repo)
        except ports.git.PrivateGitError as e:
            return _fail(ref, str(e), source_key=key)

    expect = {"status": expect_status or current["status"], **{k: current[k] for k in changes}}
    res = ports.revisions.append_revision(key, changes, reason, via, session_id, data_dir=data_dir, expect=expect)
    if not res.ok:
        if any(e.startswith("precondition failed") for e in res.errors):
            return ReviseResult(False, "skipped", ref, key,
                                "changed by someone else since it was loaded; reload and try again", errors=res.errors)
        return _fail(ref, "; ".join(res.errors), source_key=key)
    out = ReviseResult(True, "changed", ref, key, f"{verb}: {', '.join(changes)}", notes=notes, revision=res.revision)
    if repo is not None:
        try:
            out.commit = ports.git.commit_private_change([log_path], f"{verb}: {key} ({', '.join(changes)})", repo)
            out.detached = ports.git.is_detached(repo)
        except ports.git.PrivateGitError as e:
            out.ok = False
            out.commit_error = f"the revision IS in {log_path} but is NOT committed: {e}"
            out.errors.append(out.commit_error)
    return out


def edit_fact(ports, ref, reason, statement=None, trust_level=None, trust_rationale=None, recheck_by=None, notes=None,
              via="cli", session_id=None, data_dir=None, commit=True, allow_dirty=False, db=None,
              expect_status=None):
    """Append one revision changing the given fields (None = leave alone; "" clears trust_rationale,
    recheck_by, notes). The subject is immutable and the visibility is never lowered: if the new
    statement trips a privacy rule the revision also raises visibility to private. `expect_status`
    (the inbox passes 'pending') makes a fact reviewed elsewhere since it was listed a clean
    skip. The write carries an `expect` of every field it changes as it was read, so a concurrent
    edit is never silently overwritten. ONE commit in the data repo, like review.approve."""
    changes, errors = _validate_edit({"statement": statement, "trust_level": trust_level,
                                      "trust_rationale": trust_rationale, "recheck_by": recheck_by, "notes": notes})
    if errors:
        return _fail(ref, "; ".join(errors))
    if not changes:
        return _fail(ref, "nothing to edit: give at least one field")

    def adjust(key, current, ch):
        # Any edit to text the keyword rules scan (statement, notes, rationales) re-runs them on the
        # fact as it will stand, so adding a listed name in `notes` raises visibility like a new statement.
        if not any(k in ch for k in ("statement", "notes", "trust_rationale")):
            return ch, None
        subject = ports.lifecycle_reads.subjects(ports.revisions.default_data_dir() if data_dir is None else data_dir).get(key)
        rules = ports.reviews.rules_with_db_context(data_dir, db)
        ch, note = lifecycle_rules.privacy_floor_note(current, ch, subject, rules)
        if note:
            adjust.notes.append(note)
        return ch, None
    adjust.notes = []
    out = _revise(ports, ref, changes, reason, via, session_id, data_dir, commit, allow_dirty, db, expect_status, "edit", adjust)
    out.notes.extend(adjust.notes)
    return out
