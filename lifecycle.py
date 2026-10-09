"""Fact lifecycle (#8, #23): supersede, retract and set-visibility, each one appended revision.

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
import datetime
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import privacy
import privacy_store
import review
import revisions
import revisions_store
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

CHANGED_OUTCOMES = ("superseded", "retracted", "visibility_set")
OUTCOMES = CHANGED_OUTCOMES + ("unchanged", "refused", "unknown", "error")


@dataclass
class LifecycleResult:
    verb: str
    ref: str
    outcome: str = "error"                 # one of OUTCOMES
    source_key: Optional[str] = None
    reason: Optional[str] = None           # why unchanged / refused / unknown / failed
    revision: Optional[dict] = None        # the appended revision when something changed
    errors: List[str] = field(default_factory=list)   # call-level failure (blank reason, dirty tree, bad data)
    notes: List[str] = field(default_factory=list)
    commit: Optional[str] = None           # short hash when a commit was made
    commit_error: Optional[str] = None     # revision written but NOT committed
    detached: bool = False

    @property
    def changed(self):
        return self.outcome in CHANGED_OUTCOMES

    @property
    def ok(self):
        """True when the request is satisfied: a revision was written and committed (or not in a
        repo), or the fact was already in the requested state. Refused, unknown, error, a call-level
        error or a failed commit are not ok."""
        return (not self.errors and self.commit_error is None
                and self.outcome in CHANGED_OUTCOMES + ("unchanged",))

    def to_json(self):
        d = asdict(self)
        d["ok"] = self.ok
        return d


# A decision for one fact: ("write", changes, expect, outcome, note) | ("unchanged"|"refused", message)
def _write(changes, expect, outcome):
    return ("write", changes, expect, outcome)


def _no(kind, message):
    return (kind, message)


# --------------------------------------------------------------------------- decisions

def _decide_retract(states, key, ctx):
    cur = states[key]
    if cur["status"] == "retracted":
        return _no("unchanged", "already retracted")
    return _write({"status": "retracted", "superseded_by": None},
                  {"status": cur["status"], "superseded_by": cur["superseded_by"]}, "retracted")


def _decide_supersede(states, key, ctx):
    by = ctx["by_key"]
    cur = states[key]
    if cur["status"] == "pending":
        return _no("refused", "the fact is pending; approve or reject it first")
    if cur["status"] == "retracted":
        return _no("refused", "the fact is retracted; reverse that with an 'active' revision first "
                              "(revisions_store.append_revision) if it should be superseded instead")
    if by == key:
        return _no("refused", "a fact cannot supersede itself")
    rep = states[by]
    if rep["status"] == "retracted":
        return _no("refused", f"the replacement {by!r} is retracted")
    if rep["status"] == "pending":
        return _no("refused", f"the replacement {by!r} is pending; approve it first")
    seen, x = set(), by
    while x is not None and x not in seen:
        if x == key:
            return _no("refused", f"cycle: {by!r} is (indirectly) superseded by {key!r}")
        seen.add(x)
        x = states[x]["superseded_by"] if x in states else None
    if cur["status"] == "superseded" and cur["superseded_by"] == by:
        return _no("unchanged", f"already superseded by {by!r}")
    return _write({"status": "superseded", "superseded_by": by},
                  {"status": cur["status"], "superseded_by": cur["superseded_by"]}, "superseded")


def _decide_visibility(states, key, ctx):
    want = ctx["visibility"]
    cur = states[key]
    if ctx.get("raise_only") and want == "normal":
        # A caller that never lowers (the inbox): still name the rule when one floors the fact.
        floor = _floor_resolution(ctx, key, cur)
        if not isinstance(floor, str) and floor.visibility == "private":
            return _no("refused", "cannot be made normal; " + floor.explain())
        return _no("refused", "this caller only raises visibility to private; lowering is done with `set-visibility`")
    if cur["visibility"] == want:
        return _no("unchanged", f"visibility is already {want!r}")
    if want == "normal":
        floor = _floor_resolution(ctx, key, cur)
        if isinstance(floor, str):
            return _no("refused", floor)
        if floor.visibility == "private":
            return _no("refused", "cannot lower to normal, the privacy rules keep it private (the build would "
                                  "raise it again): " + floor.explain())
    return _write({"visibility": want}, {"visibility": cur["visibility"]}, "visibility_set")


def _floor_resolution(ctx, key, cur):
    """privacy.check for the fact as it stands now; a string is a refusal message."""
    db = ctx["db"]
    if db is None:
        return ("lowering to normal needs the built db (for the subject tree the privacy rules apply to); "
                "build it, or pass db=")
    entry = ctx["entries"][key]["entry"]
    subject = entry.get("subject")
    try:
        rules = privacy_store.load_rules(privacy_store.rules_path(ctx["data_dir"]))
        with revisions_store.connection(db) as con:
            rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
    except privacy.PrivacyRulesError as e:
        return f"cannot check the privacy rules: {e}"
    except sqlite3.Error as e:
        return f"cannot read the subject tree from the db: {e} (rebuild it)"
    parents = {n: p for n, p in rows}
    known = set(parents) | {e["entry"].get("subject") for e in ctx["entries"].values()
                            if isinstance(e["entry"].get("subject"), str)}
    return privacy.check(subject, cur["statement"], rules.with_context(parents=parents, known_subjects=known),
                         requested="normal")


# --------------------------------------------------------------------------- engine

def _gated(decide, states, key, ctx, expect_status):
    """`decide`, preceded by the caller's status precondition (the inbox passes 'pending': a fact
    reviewed elsewhere since it was listed is 'unchanged', never written). The status joins the
    write's `expect`, so it is also re-checked under the revision log's lock."""
    if expect_status is None:
        return decide(states, key, ctx)
    status = states[key]["status"]
    if status != expect_status:
        return _no("unchanged", f"status is {status!r}, not {expect_status!r}")
    plan = decide(states, key, ctx)
    if plan[0] == "write":
        plan = _write(plan[1], {**plan[2], "status": expect_status}, plan[3])
    return plan


def _run(verb, ref, by_ref, reason, via, session_id, data_dir, commit, allow_dirty, db, decide, ctx, message,
         expect_status=None):
    result = LifecycleResult(verb, str(ref))
    if not isinstance(reason, str) or not reason.strip():
        result.errors.append("reason is required")
        return result
    data_dir = revisions_store.default_data_dir() if data_dir is None else data_dir
    log_path = os.path.join(data_dir, revisions.REVISIONS_FILENAME)
    try:
        states = review.current_states(data_dir)
        entries = {e["key"]: e for e in revisions_store.load_entries(data_dir)}
    except revisions.RevisionError as e:
        result.errors.append(str(e))
        return result
    key, problem = review.resolve_ref(ref, states, db)
    if problem:
        result.outcome = "error" if "database" in problem else "unknown"
        result.reason = problem
        return result
    result.source_key = key
    ctx = dict(ctx, db=db, data_dir=data_dir, entries=entries)
    if by_ref is not None:
        by_key, problem = review.resolve_ref(by_ref, states, db)
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
            repo = find_repo(data_dir)
            if repo is None:
                result.notes.append(f"{data_dir} is not inside a git repository; the change will not be committed.")
            elif not allow_dirty:
                ensure_clean_tree(repo)
        except PrivateGitError as e:
            result.errors.append(str(e))
            return result

    for attempt in (1, 2):
        _, changes, expect, outcome = plan
        res = revisions_store.append_revision(key, changes, reason, via, session_id, data_dir=data_dir, expect=expect)
        if res.ok:
            result.outcome, result.revision = outcome, res.revision
            break
        if attempt == 1 and any(e.startswith("precondition failed") for e in res.errors):
            # Someone else changed the fact since we looked: decide again on the fresh state.
            try:
                states = review.current_states(data_dir)
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
            result.commit = commit_private_change([log_path], message(key, reason), repo)
            result.detached = is_detached(repo)
        except PrivateGitError as e:
            result.commit_error = f"the revision IS in {log_path} but is NOT committed: {e}"
    return result


# --------------------------------------------------------------------------- public API

def supersede(ref, by_ref, reason, via="cli", session_id=None, data_dir=None, commit=True,
              allow_dirty=False, db=None):
    """Mark `ref` superseded by `by_ref` (a fact id or source_key). `reason` is required."""
    return _run("supersede", ref, by_ref, reason, via, session_id, data_dir, commit, allow_dirty, db,
                _decide_supersede, {}, lambda key, _r: f"supersede: {key}")


def retract(ref, reason, via="cli", session_id=None, data_dir=None, commit=True, allow_dirty=False, db=None):
    """Mark `ref` retracted. `reason` is required."""
    return _run("retract", ref, None, reason, via, session_id, data_dir, commit, allow_dirty, db,
                _decide_retract, {}, lambda key, _r: f"retract: {key}")


def set_visibility(ref, visibility, reason, via="cli", session_id=None, data_dir=None, commit=True,
                   allow_dirty=False, db=None, expect_status=None, raise_only=False):
    """Set `ref`'s visibility to 'normal' or 'private'. Lowering to normal needs `db` and is refused
    when the privacy rules say private. `reason` is required.
    `expect_status` (the inbox passes 'pending'): a fact not in that status is 'unchanged', and the
    status is part of the write's precondition. `raise_only`: any request for 'normal' is refused
    (naming the privacy rule when one floors the fact); only raising to 'private' is allowed."""
    if visibility not in ("normal", "private"):
        return LifecycleResult("set-visibility", str(ref), errors=[f"visibility {visibility!r} must be 'normal' or 'private'"])
    return _run("set-visibility", ref, None, reason, via, session_id, data_dir, commit, allow_dirty, db,
                _decide_visibility, {"visibility": visibility, "raise_only": raise_only},
                lambda key, _r: f"set-visibility: {key} {visibility}", expect_status)


# --------------------------------------------------------------------------- edit (#33)
# Moved from inbox.py so the serving layer holds no write logic; behavior unchanged. `edit` (the
# CLI) and the inbox page both call edit_fact.

@dataclass
class ReviseResult:
    ok: bool
    outcome: str                       # 'changed' | 'skipped' | 'refused' | 'error'
    ref: str = ""
    source_key: Optional[str] = None
    message: str = ""                  # one line for a person
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    revision: Optional[dict] = None
    commit: Optional[str] = None
    commit_error: Optional[str] = None
    detached: bool = False

    def to_dict(self):
        return {"ok": self.ok, "outcome": self.outcome, "ref": self.ref, "source_key": self.source_key,
                "message": self.message, "errors": self.errors, "notes": self.notes, "revision": self.revision,
                "commit": self.commit, "commit_error": self.commit_error}


def _fail(ref, message, outcome="error", source_key=None):
    return ReviseResult(False, outcome, str(ref), source_key, message, errors=[message])


def _subjects(data_dir):
    return {e["key"]: e["entry"].get("subject") for e in revisions_store.load_entries(data_dir)}


def _validate_edit(changes):
    """Normalize and validate requested edits -> (changes, errors). A blank rationale / recheck /
    notes clears the field; a blank statement is refused."""
    errors, out = [], {}
    for name, value in changes.items():
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{name} must be text")
            continue
        if name == "statement":
            if not value.strip():
                errors.append("statement must not be blank")
                continue
            out[name] = value.strip()
        elif name == "trust_level":
            if value not in revisions.VALID_TRUST:
                errors.append(f"trust level {value!r} must be one of {sorted(revisions.VALID_TRUST)}")
                continue
            out[name] = value
        elif name == "recheck_by":
            text = value.strip()
            if text:
                try:
                    datetime.date.fromisoformat(text)
                except ValueError:
                    errors.append(f"recheck-by {value!r} must be a date as YYYY-MM-DD")
                    continue
            out[name] = text or None
        else:
            out[name] = value.strip() or None
    return out, errors


def _revise(ref, changes, reason, via, session_id, data_dir, commit, allow_dirty, db, expect_status, verb, adjust=None):
    """Resolve, check, one locked write, one commit.
    `adjust(key, current, changes) -> (changes, ReviseResult | None)` may add fields or refuse."""
    ref = str(ref)
    if not isinstance(reason, str) or not reason.strip():
        return _fail(ref, "reason is required")
    data_dir = revisions_store.default_data_dir() if data_dir is None else data_dir
    try:
        states = review.current_states(data_dir)
    except revisions.RevisionError as e:
        return _fail(ref, str(e))
    try:
        key, problem = review.resolve_ref(ref, states, db)
    except sqlite3.Error as e:
        return _fail(ref, f"could not read the database to resolve fact id {ref}: {e}")
    if problem:
        return _fail(ref, problem, "error")
    current = states[key]
    if expect_status is not None and current["status"] != expect_status:
        return ReviseResult(True, "skipped", ref, key, f"already {review.status_word(current['status'])}: nothing changed")
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
            repo = find_repo(data_dir)
            if repo is None:
                notes.append(f"{data_dir} is not inside a git repository; the change will not be committed.")
            elif not allow_dirty:
                ensure_clean_tree(repo)
        except PrivateGitError as e:
            return _fail(ref, str(e), source_key=key)

    expect = {"status": expect_status or current["status"], **{k: current[k] for k in changes}}
    res = revisions_store.append_revision(key, changes, reason, via, session_id, data_dir=data_dir, expect=expect)
    if not res.ok:
        if any(e.startswith("precondition failed") for e in res.errors):
            return ReviseResult(False, "skipped", ref, key,
                                "changed by someone else since it was loaded; reload and try again", errors=res.errors)
        return _fail(ref, "; ".join(res.errors), source_key=key)
    out = ReviseResult(True, "changed", ref, key, f"{verb}: {', '.join(changes)}", notes=notes, revision=res.revision)
    if repo is not None:
        try:
            out.commit = commit_private_change([log_path], f"{verb}: {key} ({', '.join(changes)})", repo)
            out.detached = is_detached(repo)
        except PrivateGitError as e:
            out.ok = False
            out.commit_error = f"the revision IS in {log_path} but is NOT committed: {e}"
            out.errors.append(out.commit_error)
    return out


def edit_fact(ref, reason, statement=None, trust_level=None, trust_rationale=None, recheck_by=None, notes=None,
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
        subject = _subjects(revisions_store.default_data_dir() if data_dir is None else data_dir).get(key)
        rules = review.rules_with_db_context(data_dir, db)
        merged = {**current, **ch}
        res = privacy.resolve_visibility(subject, merged["statement"], current["visibility"], rules,
                                         extra_text=(merged.get("notes"), merged.get("trust_rationale"),
                                                     merged.get("recheck_rationale")))
        if res.visibility == "private" and current["visibility"] != "private":
            ch["visibility"] = "private"
            adjust.notes.append(f"visibility raised to private: {res.explain()}")
        return ch, None
    adjust.notes = []
    out = _revise(ref, changes, reason, via, session_id, data_dir, commit, allow_dirty, db, expect_status, "edit", adjust)
    out.notes.extend(adjust.notes)
    return out
