"""Apply the CURRENT privacy rules to every fact and subject of a built database (the privacy floor, #31).

Raise-only: a fact stored private stays private even when the rule that once caught it is gone. A subject is
marked private when its own tag or an ancestor's is. The rules themselves are `privacy` (domain); this use case
reads the rows through `FactPrivacyTable`, asks the domain what each fact resolves to, and writes only raises.
It does not commit; the caller owns the transaction.
"""
from dataclasses import dataclass

import privacy
from fact_privacy_table_port import FactPrivacyTable


@dataclass(frozen=True)
class PrivacyFloorDeps:
    privacy_table: FactPrivacyTable


def apply_privacy_floor(deps, rules):
    """Returns {"raised": [(fact_id, explanation), ...], "private_subjects": n}."""
    table = deps.privacy_table
    parents = table.subject_parents()
    ctx = rules.with_context(parents=parents, known_subjects=set(parents))
    private_subjects = 0
    for name in parents:
        flag = privacy.subject_is_private(name, ctx)
        table.mark_subject(name, flag)
        private_subjects += 1 if flag else 0
    raised = []
    for fact in table.facts_to_scan():
        res = privacy.resolve_visibility(fact.subject, fact.statement, fact.visibility, ctx, extra_text=fact.extra_text)
        if res.visibility == "private" and fact.visibility != "private":
            table.raise_to_private(fact.fact_id)
            raised.append((fact.fact_id, res.explain()))
    return {"raised": raised, "private_subjects": private_subjects}
