"""Review of pending facts (#6), the rules: result types and the pure decisions behind the queue.

DOMAIN (import-linter contract "domain-has-no-infrastructure"): no file, db or git access. `review`
is the use case that orchestrates the adapters (`review_store`, `revisions_store`, `privacy_store`,
`private_git`) and calls these.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import privacy

NO_ROW = object()  # review_store.source_key_for_id: "there is no such fact" (as opposed to None = no source_key)
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


def status_word(status):
    """A revision status as the review UI words it: active -> approved, retracted -> rejected."""
    return {"active": "approved", "retracted": "rejected", "pending": "pending"}.get(status, status)


def pending_keys(states, entries_order):
    return [k for k in entries_order if states[k]["status"] == "pending"]


def visibility_basis(subject, statement, visibility, rules):
    """Which rule is behind a stored visibility, as one line."""
    res = privacy.check(subject, statement, rules, requested="normal")
    if res.visibility == "private":
        return "private by rule: " + "; ".join(str(r) for r in res.raised_by)
    if visibility == "private":
        return "private by request or default (no privacy rule applies)"
    return "normal: no privacy rule applies"


def parse_ref(ref):
    """Classify a ref: ("bad", problem) | ("id", int) | ("key", text). Pure; the id needs a db."""
    if isinstance(ref, bool) or not isinstance(ref, (int, str)):
        return "bad", "ref must be a fact id or a source_key"
    text = str(ref).strip()
    if not text:
        return "bad", "blank reference"
    if text.isdigit():
        return "id", int(text)
    return "key", text


def key_from_id_row(fact_id, source_key, states):
    """(source_key | None, problem | None) for a fact id whose db row gave `source_key` (None = no row)."""
    if source_key is NO_ROW:
        return None, f"no fact with id {fact_id}"
    if source_key is None or source_key not in states:
        return None, f"fact {fact_id} has no source_key in the data files"
    return source_key, None


def key_in_states(text, states):
    if text not in states:
        return None, f"no fact with source_key {text!r}"
    return text, None


def overlay_pending(row, cur, rules):
    """The review-page fact for pending db `row`, overlaid with its current state `cur` (None when the
    data files do not know the key). `rules` are the privacy rules (with db context)."""
    fact = dict(row)
    if cur is not None:
        fact.update(statement=cur["statement"], trust_level=cur["trust_level"], visibility=cur["visibility"],
                    trust_rationale=cur["trust_rationale"], recheck_by=cur["recheck_by"], notes=cur["notes"])
    else:
        fact.update(trust_rationale=None, recheck_by=None, notes=None)
    key = row["source_key"]
    fact["ref"] = str(key if key else row["id"])
    fact["basis"] = visibility_basis(row["subject"], fact["statement"], fact["visibility"], rules)
    return fact
