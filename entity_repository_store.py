"""SQLite adapter for the build's `EntityRepository` (`build_repositories_port`): the entities file as values, the
entity and alias rows, and the fact-entity links.

It stores what it is told and decides nothing: which facts mention which entities, and whether a private mention is
stored private, are `entity_link_rules` and `entity_link_service`. Ids are allocated in call order (the ordering
contract in `build_repositories_port`): an entity row when `add` is called, then its alias rows. It never commits;
the step owns the transaction.
"""
from typing import List, Optional, Sequence

import build_records as r
import entities_store
import entity_rules


def read_file(path: str) -> Optional[Sequence[entity_rules.Entity]]:
    """entities.json as values, None when the file is absent; a bad file raises `entity_rules.EntitiesError`."""
    found = entities_store.read_file(path)
    return None if found is None else [entity_rules.Entity.from_dict(e) for e in found]


class SqliteEntityRepository:
    def __init__(self, con):
        self._con = con

    def read_file(self, path: str) -> Optional[Sequence[entity_rules.Entity]]:
        return read_file(path)

    def add(self, entity: entity_rules.Entity) -> r.EntityRowId:
        name_norm, *alias_norms = entity.terms
        cur = self._con.execute(
            "INSERT INTO entities (entity_key, canonical_name, name_norm, type, private, external_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity.id, entity.canonical_name, name_norm, entity.type, 1 if entity.private else 0,
             entity.external_id, entity.notes))
        row_id = r.EntityRowId(cur.lastrowid)
        for alias, alias_norm in zip(entity.aliases, alias_norms):
            self._con.execute("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (?, ?, ?)",
                              (row_id, alias, alias_norm))
        return row_id

    def fact_texts(self) -> List[r.FactText]:
        """Every fact's free text (the set the privacy resolver scans), in fact id order, as of this call."""
        rows = self._con.execute(
            """SELECT f.id, f.statement, f.notes, f.trust_rationale, f.recheck_rationale, f.source_quote, f.applies_to,
                      (SELECT group_concat(COALESCE(fs.locator, '') || ' ' || COALESCE(fs.quote, ''), char(10))
                       FROM fact_sources fs WHERE fs.fact_id = f.id)
               FROM facts f ORDER BY f.id""").fetchall()
        return [r.FactText(r.FactId(fact_id), tuple(texts)) for fact_id, *texts in rows]

    def link_fact(self, fact_id: r.FactId, entity_row_id: r.EntityRowId) -> None:
        self._con.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fact_id, entity_row_id))

    def fact_visibility(self, fact_id: r.FactId) -> Optional[str]:
        row = self._con.execute("SELECT visibility FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return None if row is None else row[0]
