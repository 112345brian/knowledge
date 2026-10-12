"""Subject hierarchy use cases: load the hierarchy file's entries into the stored subjects, and find (or create)
the subject a fact is filed under. It talks to the build's `SubjectRepository` (`build_repositories_port`); every
decision is `subject_rules` (what an entry defaults to, which alias may be claimed, which subject a name means)
or `identity_service.SubjectIdentity`; every read and write is the repository."""
from dataclasses import dataclass
from typing import Dict, Optional

import build_records as r
import identity_service
import subject_rules
from build_repositories_port import AliasRejected, SubjectRepository


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
        repo.ensure(p.name, p.domain)
    ids = repo.ids_by_name()
    for p in plans:
        parent = ids[p.parent] if p.parent else None
        repo.set_hierarchy(ids[p.name], parent, p.relation, p.description, p.deprecated)
    names = {subject_id: name for name, subject_id in ids.items()}
    for p in plans:  # second pass: the replacement may be listed after the subject
        if p.replaced_by:
            repo.set_replacement(ids[p.name], ids[p.replaced_by])
        for alias in p.aliases:
            owner = repo.find_by_alias(alias)
            claimed = {} if owner is None else {alias: names.get(owner, str(owner))}
            reason = subject_rules.refuse_alias(alias, ids, claimed)
            if reason is None:
                try:
                    repo.add_alias(ids[p.name], alias)
                except AliasRejected as err:
                    reason = err.reason
            if reason is not None:
                raise ValueError(f"subjects.json: alias {alias!r} of {p.name!r} cannot be loaded ({reason}); "
                                 "it equals an existing subject name or another alias") from None
    return subject_rules.HierarchyTally(repo.count_with_parent(), repo.count_aliases(), repo.count_deprecated())


def subject_for_fact(deps: HierarchyDeps, name, domain: Optional[str] = None,
                     cache: Optional[Dict[str, r.SubjectId]] = None, resolve_aliases: bool = True) -> r.SubjectId:
    """The id of the subject a fact filed under `name` belongs to: the subject of that name, else the subject that
    claims it as an alias (only when `resolve_aliases`, #43), else a new subject (in `domain`, or the store's default
    when None). `cache` memoizes by the name given."""
    if cache is not None and name in cache:
        return cache[name]
    subject_id = identity_service.SubjectIdentity(deps.subjects).resolve(name, domain=domain, via_aliases=resolve_aliases)
    if cache is not None:
        cache[name] = subject_id
    return subject_id
