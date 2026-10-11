"""A fact's lifecycle as types with behaviour: `Status`, `Visibility` and `FactState`.

Model ring: imports nothing else in the repo. The status machine (pending -> active -> superseded / retracted),
who may replace whom, and how a visibility change reads used to be string comparisons repeated in
`lifecycle_rules` and `review_service`; they live here once, with the refusal wording the CLI and the inbox
show. `Status` and `Visibility` are `str` enums, so a value read from JSON or the db still compares equal to
its plain-string form and nothing that handles dicts has to change.
"""
import enum
from dataclasses import dataclass
from typing import Mapping, Optional


class Status(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"

    @property
    def review_word(self):
        """How the review page words it: active -> approved, retracted -> rejected."""
        return {"active": "approved", "retracted": "rejected"}.get(self.value, self.value)


class Visibility(str, enum.Enum):
    NORMAL = "normal"
    PRIVATE = "private"


def review_word(status):
    """`status` as the review UI words it; a value that is not a known status is returned unchanged."""
    try:
        return Status(status).review_word
    except ValueError:
        return status


@dataclass(frozen=True)
class FactState:
    """What the lifecycle decisions need to know about a fact as it stands. Built from the state row the
    revision log yields; the row (a dict) stays the transport, this is how the rules read it."""
    status: str
    visibility: str = Visibility.PRIVATE.value
    superseded_by: Optional[str] = None
    statement: str = ""

    @classmethod
    def of(cls, row):
        return cls(status=row["status"], visibility=row.get("visibility", Visibility.PRIVATE.value),
                   superseded_by=row.get("superseded_by"), statement=row.get("statement", ""))

    @property
    def is_pending(self):
        return self.status == Status.PENDING

    @property
    def is_retracted(self):
        return self.status == Status.RETRACTED

    @property
    def is_superseded(self):
        return self.status == Status.SUPERSEDED

    def why_not_supersedable(self):
        """The refusal wording when this fact cannot be superseded yet, else None."""
        if self.is_pending:
            return "the fact is pending; approve or reject it first"
        if self.is_retracted:
            return ("the fact is retracted; reverse that with an 'active' revision first "
                    "(revisions_store.append_revision) if it should be superseded instead")
        return None

    def why_not_a_replacement(self, key):
        """The refusal wording when this fact cannot be the replacement `key`, else None."""
        if self.is_retracted:
            return f"the replacement {key!r} is retracted"
        if self.is_pending:
            return f"the replacement {key!r} is pending; approve it first"
        return None

    def is_superseded_by(self, key):
        return self.is_superseded and self.superseded_by == key

    def visibility_change_to(self, want):
        """"unchanged" | "lower" (private -> normal, which the privacy rules may refuse) | "raise"."""
        if self.visibility == want:
            return "unchanged"
        return "lower" if want == Visibility.NORMAL else "raise"


def supersession_cycle(states: Mapping[str, Mapping], key, by):
    """True when making `by` the replacement of `key` would loop: following `by`'s own replacement chain
    arrives back at `key`."""
    seen, x = set(), by
    while x is not None and x not in seen:
        if x == key:
            return True
        seen.add(x)
        x = states[x].get("superseded_by") if x in states else None
    return False
