"""SQLite adapter for the entity-link build pass over a built database.

`SqliteEntityLinkTable` binds one connection and implements `entity_link_table_port.EntityLinkTable`. It only reads
and writes rows; which facts mention which entities is decided by the domain and the use case. The revision and
privacy-floor rows are the foundation's repositories (`build_repositories_port`): `revisions_store.SqliteRevisionRepository`
and `privacy_store.SqlitePrivacyRepository`. It never commits.
"""
from entity_link_table_port import FactMention


class SqliteEntityLinkTable:
    def __init__(self, con):
        self._con = con

    def add_entity(self, entity, name_norm):
        cur = self._con.execute(
            "INSERT INTO entities (entity_key, canonical_name, name_norm, type, private, external_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity["id"], entity["canonical_name"], name_norm, entity["type"], 1 if entity["private"] else 0,
             entity["external_id"], entity["notes"]))
        return cur.lastrowid

    def add_alias(self, entity_row, alias, alias_norm):
        self._con.execute("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (?, ?, ?)",
                          (entity_row, alias, alias_norm))

    def fact_mentions(self):
        rows = self._con.execute(
            """SELECT f.id, f.visibility, f.statement, f.notes, f.trust_rationale, f.recheck_rationale, f.source_quote,
                      f.applies_to,
                      (SELECT group_concat(COALESCE(fs.locator, '') || ' ' || COALESCE(fs.quote, ''), char(10))
                       FROM fact_sources fs WHERE fs.fact_id = f.id)
               FROM facts f ORDER BY f.id""").fetchall()
        return [FactMention(fact_id, tuple(texts), visibility) for fact_id, visibility, *texts in rows]

    def link(self, fact_id, entity_row):
        self._con.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fact_id, entity_row))
