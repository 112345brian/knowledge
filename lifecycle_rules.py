"""Fact lifecycle (#8, #23, #33), the rules: result types and the pure decisions.

DOMAIN (import-linter contract "domain-has-no-infrastructure"): no file, db, git or clock access.
Given the current state of the facts, these decide whether a supersede / retract / set-visibility
is a write, unchanged or refused, and validate an edit. `lifecycle` is the use case that loads the
state (through `review_store`, `revisions_store`, `lifecycle_store`), calls these, writes the
revision and commits.
"""
import datetime
from dataclasses import asdict, dataclass, field
from typing import List, Optional

import privacy
from fact_rules import VALID_TRUST


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
def write(changes, expect, outcome):
    return ("write", changes, expect, outcome)


def no(kind, message):
    return (kind, message)


# --------------------------------------------------------------------------- decisions

def decide_retract(states, key, ctx):
    cur = states[key]
    if cur["status"] == "retracted":
        return no("unchanged", "already retracted")
    return write({"status": "retracted", "superseded_by": None},
                  {"status": cur["status"], "superseded_by": cur["superseded_by"]}, "retracted")


def decide_supersede(states, key, ctx):
    by = ctx["by_key"]
    cur = states[key]
    if cur["status"] == "pending":
        return no("refused", "the fact is pending; approve or reject it first")
    if cur["status"] == "retracted":
        return no("refused", "the fact is retracted; reverse that with an 'active' revision first "
                              "(revisions_store.append_revision) if it should be superseded instead")
    if by == key:
        return no("refused", "a fact cannot supersede itself")
    rep = states[by]
    if rep["status"] == "retracted":
        return no("refused", f"the replacement {by!r} is retracted")
    if rep["status"] == "pending":
        return no("refused", f"the replacement {by!r} is pending; approve it first")
    seen, x = set(), by
    while x is not None and x not in seen:
        if x == key:
            return no("refused", f"cycle: {by!r} is (indirectly) superseded by {key!r}")
        seen.add(x)
        x = states[x]["superseded_by"] if x in states else None
    if cur["status"] == "superseded" and cur["superseded_by"] == by:
        return no("unchanged", f"already superseded by {by!r}")
    return write({"status": "superseded", "superseded_by": by},
                  {"status": cur["status"], "superseded_by": cur["superseded_by"]}, "superseded")


def decide_visibility(states, key, ctx):
    want = ctx["visibility"]
    cur = states[key]
    if ctx.get("raise_only") and want == "normal":
        # A caller that never lowers (the inbox): still name the rule when one floors the fact.
        floor = floor_resolution(ctx, key, cur)
        if not isinstance(floor, str) and floor.visibility == "private":
            return no("refused", "cannot be made normal; " + floor.explain())
        return no("refused", "this caller only raises visibility to private; lowering is done with `set-visibility`")
    if cur["visibility"] == want:
        return no("unchanged", f"visibility is already {want!r}")
    if want == "normal":
        floor = floor_resolution(ctx, key, cur)
        if isinstance(floor, str):
            return no("refused", floor)
        if floor.visibility == "private":
            return no("refused", "cannot lower to normal, the privacy rules keep it private (the build would "
                                  "raise it again): " + floor.explain())
    return write({"visibility": want}, {"visibility": cur["visibility"]}, "visibility_set")


def floor_resolution(ctx, key, cur):
    """privacy.check for the fact as it stands now; a string is a refusal message. `ctx["floor_inputs"]`
    is a callable returning (rules, parents) or a refusal string (the use case reads the rules file and
    the db's subject tree through lifecycle_store)."""
    loaded = ctx["floor_inputs"]()
    if isinstance(loaded, str):
        return loaded
    rules, parents = loaded
    subject = ctx["entries"][key]["entry"].get("subject")
    known = set(parents) | {e["entry"].get("subject") for e in ctx["entries"].values()
                            if isinstance(e["entry"].get("subject"), str)}
    return privacy.check(subject, cur["statement"], rules.with_context(parents=parents, known_subjects=known),
                         requested="normal")


def gated(decide, states, key, ctx, expect_status):
    """`decide`, preceded by the caller's status precondition (the inbox passes 'pending': a fact
    reviewed elsewhere since it was listed is 'unchanged', never written). The status joins the
    write's `expect`, so it is also re-checked under the revision log's lock."""
    if expect_status is None:
        return decide(states, key, ctx)
    status = states[key]["status"]
    if status != expect_status:
        return no("unchanged", f"status is {status!r}, not {expect_status!r}")
    plan = decide(states, key, ctx)
    if plan[0] == "write":
        plan = write(plan[1], {**plan[2], "status": expect_status}, plan[3])
    return plan


# --------------------------------------------------------------------------- edit (#33)

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


def fail(ref, message, outcome="error", source_key=None):
    return ReviseResult(False, outcome, str(ref), source_key, message, errors=[message])


def validate_edit(changes):
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
            if value not in VALID_TRUST:
                errors.append(f"trust level {value!r} must be one of {sorted(VALID_TRUST)}")
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


def privacy_floor_note(current, changes, subject, rules):
    """For an edit that touches text the keyword rules scan: (changes, note). If the fact as it will
    stand trips a privacy rule while stored non-private, the revision also raises visibility."""
    if not any(k in changes for k in ("statement", "notes", "trust_rationale")):
        return changes, None
    merged = {**current, **changes}
    res = privacy.resolve_visibility(subject, merged["statement"], current["visibility"], rules,
                                     extra_text=(merged.get("notes"), merged.get("trust_rationale"),
                                                 merged.get("recheck_rationale"), merged.get("applies_to")))
    if res.visibility == "private" and current["visibility"] != "private":
        return {**changes, "visibility": "private"}, f"visibility raised to private: {res.explain()}"
    return changes, None
