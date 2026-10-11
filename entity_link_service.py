"""Link facts to the entities they mention in a built database (#42), and prove no private mention is public.

The matching and the invariant are `entity_link_rules` (domain); this use case writes the entities and their
aliases, reads each fact's text through `EntityLinkTable`, records the links the domain finds, and reports the
facts that mention a private entity without being stored private. `link_entities` is the build step's entry:
it refuses (UnprotectedPrivateMention) rather than return that list, so a caller cannot discard it.
It does not commit; the caller owns the transaction.
"""
from dataclasses import dataclass
from typing import List, Sequence

import entity_link_rules
import textmatch
from entity_link_rules import UnprotectedPrivateMention
from entity_link_table_port import EntityLinkTable


@dataclass(frozen=True)
class EntityLinkDeps:
    link_table: EntityLinkTable


@dataclass(frozen=True)
class LinkResult:
    entities: int
    links: int
    unprotected: List[int]

    def as_dict(self):
        return {"entities": self.entities, "links": self.links, "unprotected": list(self.unprotected)}


def link_facts(deps, entities: Sequence[dict]) -> LinkResult:
    """Insert `entities` and their aliases, then a link per (fact, mentioned entity). `unprotected` lists the
    facts that mention a private entity but are not stored private; use `link_entities` to refuse on them."""
    table = deps.link_table
    rows = {}
    for e in entities:
        rows[e["id"]] = table.add_entity(e, textmatch.normalize_term(e["canonical_name"]))
        for a in e["aliases"]:
            table.add_alias(rows[e["id"]], a, textmatch.normalize_term(a))
    private_ids = entity_link_rules.private_entity_ids(entities)
    links, unprotected = 0, []
    for fact in table.fact_mentions():
        hits = entity_link_rules.mentioned(fact.texts, entities)
        for eid in hits:
            table.link(fact.fact_id, rows[eid])
            links += 1
        if entity_link_rules.is_unprotected(hits, private_ids, fact.visibility):
            unprotected.append(fact.fact_id)
    return LinkResult(len(entities), links, unprotected)


def link_entities(deps, entities: Sequence[dict]) -> LinkResult:
    """`link_facts`, then raise UnprotectedPrivateMention if any fact mentions a private entity without being private."""
    result = link_facts(deps, entities)
    if result.unprotected:
        raise UnprotectedPrivateMention(result.unprotected)
    return result
