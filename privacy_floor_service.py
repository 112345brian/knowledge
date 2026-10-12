"""Apply the CURRENT privacy rules to every fact and subject of a built database (the privacy floor, #31).

Raise-only: a fact stored private stays private even when the rule that once caught it is gone. A subject is
marked private when its own tag or an ancestor's is. The rules themselves are `privacy` (domain); this use case
reads the rows through `FloorRows`, asks the domain what each fact resolves to, and writes only raises. It does
not commit; the caller owns the transaction.

`FloorRows` is the narrow row access this one pass needs, declared here because only this use case calls it. It
is the inside of `build_repositories_port.PrivacyRepository.apply_rules`: the SQLite repository (`privacy_store`)
answers `apply_rules` by running `apply_privacy_floor` over its own rows, so the decision (what a fact resolves
to) lives here and the adapter only reads and writes rows.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Tuple

import build_records as r
import privacy


@dataclass(frozen=True)
class FloorFact:
    """A fact as the privacy resolver reads it: its subject, statement, stored visibility and every other
    stored free-text field (notes, rationales, quote, applies_to, the citations' locators and quotes)."""
    fact_id: r.FactId
    subject: str
    statement: str
    visibility: str
    extra_text: Tuple[Optional[str], ...] = ()


class FloorRows(Protocol):
    def subject_parents(self) -> Dict[str, Optional[str]]: ...
    def mark_subject(self, name: str, private: bool) -> None: ...
    def facts_to_scan(self) -> List[FloorFact]: ...
    def raise_to_private(self, fact_id: r.FactId) -> None: ...


@dataclass(frozen=True)
class PrivacyFloorDeps:
    floor_rows: FloorRows


def apply_privacy_floor(deps: PrivacyFloorDeps, rules: privacy.Rules) -> r.PrivacyOutcome:
    """The facts raised to private as (fact id, explanation), in the order they were scanned, and how many
    subjects carry the private flag."""
    rows = deps.floor_rows
    parents = rows.subject_parents()
    ctx = rules.with_context(parents=parents, known_subjects=set(parents))
    private_subjects = 0
    for name in parents:
        flag = privacy.subject_is_private(name, ctx)
        rows.mark_subject(name, flag)
        private_subjects += 1 if flag else 0
    raised = []
    for fact in rows.facts_to_scan():
        res = privacy.resolve_visibility(fact.subject, fact.statement, fact.visibility, ctx, extra_text=fact.extra_text)
        if res.visibility == "private" and fact.visibility != "private":
            rows.raise_to_private(fact.fact_id)
            raised.append((fact.fact_id, res.explain()))
    return r.PrivacyOutcome(tuple(raised), private_subjects)
