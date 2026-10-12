"""Link facts to the entities they mention in a built database (#42), and prove no private mention is public.

The matching and the invariant are `entity_link_rules` (domain); this use case adds the entities (and their
aliases) through the build's `EntityRepository` (`build_repositories_port`), reads each fact's text, records the
links the domain finds, and reports the facts that mention a private entity without being stored private.
`link_entities` is the build step's entry: it refuses (UnprotectedPrivateMention) rather than return that list,
so a caller cannot discard it. It does not commit; the caller owns the transaction.
"""
from dataclasses import dataclass
from typing import Dict, List, Sequence

import entity_link_rules
from build_repositories_port import EntityRepository
from build_records import EntityRowId, FactId
from entity_link_rules import UnprotectedPrivateMention
from entity_rules import Entity


@dataclass(frozen=True)
class EntityLinkDeps:
    entities: EntityRepository


@dataclass(frozen=True)
class LinkResult:
    entities: int
    links: int
    unprotected: List[FactId]

    def as_dict(self):
        return {"entities": self.entities, "links": self.links, "unprotected": list(self.unprotected)}


def link_facts(deps: EntityLinkDeps, entities: Sequence[Entity]) -> LinkResult:
    """Add `entities`, then a link per (fact, mentioned entity). `unprotected` lists the facts that mention a
    private entity but are not stored private; use `link_entities` to refuse on them."""
    repo = deps.entities
    rows: Dict[str, EntityRowId] = {e.id: repo.add(e) for e in entities}
    as_dicts = [e.to_dict() for e in entities]
    private_ids = entity_link_rules.private_entity_ids(as_dicts)
    links, unprotected = 0, []
    for fact in repo.fact_texts():
        hits = entity_link_rules.mentioned(fact.texts, as_dicts)
        for eid in hits:
            repo.link_fact(fact.fact_id, rows[eid])
            links += 1
        visibility = repo.fact_visibility(fact.fact_id) if hits else None   # only a mention can be unprotected
        if entity_link_rules.is_unprotected(hits, private_ids, visibility):
            unprotected.append(fact.fact_id)
    return LinkResult(len(entities), links, unprotected)


def link_entities(deps: EntityLinkDeps, entities: Sequence[Entity]) -> LinkResult:
    """`link_facts`, then raise UnprotectedPrivateMention if any fact mentions a private entity without being private."""
    result = link_facts(deps, entities)
    if result.unprotected:
        raise UnprotectedPrivateMention(result.unprotected)
    return result
