"""Subject hierarchy use cases: load the hierarchy file's entries into the stored subjects, and find (or create)
the subject a fact is filed under. Every decision is `subject_rules`; every read and write is the repository."""
from dataclasses import dataclass
from typing import Dict, Optional

import subject_rules
from subject_repository_port import SubjectRepository


@dataclass(frozen=True)
class HierarchyDeps:
    """Everything this service reaches the world through; the step's context supplies it."""
    subjects: SubjectRepository


def load_hierarchy(deps: HierarchyDeps, entries) -> subject_rules.HierarchyTally:
    """Create each listed subject that is missing (a stored one keeps its domain), set parent, relation, description
    and deprecation, then replacements and alias claims. An alias that cannot be claimed raises ValueError naming it.
    Returns what the store holds afterwards."""
    repo = deps.subjects
    plans = subject_rules.plan_hierarchy(entries)
    for p in plans:
        repo.ensure_subject(p.name, p.domain)
    ids = repo.subject_ids()
    for p in plans:
        parent = ids[p.parent] if p.parent else None
        repo.place(ids[p.name], parent, p.relation, p.description, p.deprecated)
    claimed = repo.alias_owners()
    for p in plans:  # second pass: the replacement may be listed after the subject
        if p.replaced_by:
            repo.replace_with(ids[p.name], ids[p.replaced_by])
        for alias in p.aliases:
            try:
                reason = subject_rules.refuse_alias(alias, ids, claimed)
                if reason is not None:
                    raise subject_rules.AliasRefused(reason)
                repo.add_alias(ids[p.name], alias)
            except subject_rules.AliasRefused as err:
                raise ValueError(f"subjects.json: alias {alias!r} of {p.name!r} cannot be loaded ({err.reason}); "
                                 "it equals an existing subject name or another alias") from None
            claimed[alias] = p.name
    return repo.tally()


def subject_for_fact(deps: HierarchyDeps, name, domain: Optional[str] = None, cache: Optional[Dict[str, int]] = None,
                     resolve_aliases: bool = True) -> int:
    """The id of the subject a fact filed under `name` belongs to: the subject of that name, else the subject that
    claims it as an alias (only when `resolve_aliases`, #43), else a new subject (in `domain`, or the store's default
    when None). `cache` memoizes by the name given."""
    if cache is not None and name in cache:
        return cache[name]
    repo = deps.subjects
    key, _via = subject_rules.identify(name, repo.find_subject, repo.find_alias if resolve_aliases else (lambda _alias: None))
    subject_id = key if key is not None else repo.create_subject(name, domain)
    if cache is not None:
        cache[name] = subject_id
    return subject_id
