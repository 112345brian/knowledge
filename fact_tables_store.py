"""SQLite adapters for the build-time passes over a built database: revisions and the privacy floor.

Each class binds one connection and implements a port from `fact_revision_table_port` or
`fact_privacy_table_port`. They only read and write rows; which fields a revision sets and what a rule raises are
decided by the domain and the use cases.
Nothing here commits except `SqliteFactRevisionTable.commit`, which the revision use case calls.
"""
import revisions
from fact_privacy_table_port import FactText

_INSERT_REVISION_SQL = (
    "INSERT INTO fact_revisions (fact_id, " + ", ".join(revisions.REVISION_KEYS) + ") VALUES (?, "
    + ", ".join("?" for _ in revisions.REVISION_KEYS) + ")"
)

# The facts row a revision sets. Every column named here is in revision_apply_rules.STATE_FIELDS.
_WRITE_STATE_SQL = """UPDATE facts SET statement = ?, trust_level = ?, trust_rationale = ?, status = ?, visibility = ?,
                                superseded_by_fact_id = ?, recheck_by = ?, recheck_rationale = ?, freshness = ?, notes = ?,
                                kind = ?, valid_from = ?, valid_to = ?, applies_to = ?
               WHERE id = ?"""
_STATE_ORDER = ("statement", "trust_level", "trust_rationale", "status", "visibility", "superseded_by_fact_id",
                "recheck_by", "recheck_rationale", "freshness", "notes", "kind", "valid_from", "valid_to", "applies_to")

_CHUNK = 500   # well under SQLite's bound-parameter limit


def insert_revision(cur, fact_id, revision):
    """One revision snapshot into `fact_revisions` (revision 1 from the loading steps, and the log's records)."""
    cur.execute(_INSERT_REVISION_SQL, (fact_id, *[revision[k] for k in revisions.REVISION_KEYS]))


class SqliteFactRevisionTable:
    def __init__(self, con):
        self._con = con

    def fact_ids_by_source_key(self):
        return {k: i for i, k in self._con.execute("SELECT id, source_key FROM facts WHERE source_key IS NOT NULL")}

    def original_revisions(self):
        cols = ["changed_at", "freshness", *revisions.LEGACY_INHERITED]
        rows = self._con.execute("SELECT source_key, " + ", ".join(cols) + " FROM fact_revisions WHERE revision = 1")
        return {key: dict(zip(cols, vals)) for key, *vals in rows}

    def record_revision(self, fact_id, revision):
        insert_revision(self._con.cursor(), fact_id, revision)

    def write_current_state(self, fact_id, state):
        self._con.execute(_WRITE_STATE_SQL, (*[state[f] for f in _STATE_ORDER], fact_id))

    def visibility_by_fact(self):
        return {i: v for i, v in self._con.execute("SELECT id, visibility FROM facts")}

    def raise_history_to_private(self, fact_ids):
        ids = list(fact_ids)
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start:start + _CHUNK]
            self._con.execute("UPDATE fact_revisions SET visibility = 'private' WHERE visibility != 'private' "
                              f"AND fact_id IN ({', '.join('?' for _ in chunk)})", chunk)

    def revision_count(self):
        return self._con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0]

    def commit(self):
        self._con.commit()


class SqliteFactPrivacyTable:
    def __init__(self, con):
        self._con = con

    def subject_parents(self):
        rows = self._con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
        return {name: parent for name, parent in rows}

    def mark_subject(self, name, private):
        self._con.execute("UPDATE subjects SET private = ? WHERE name = ?", (1 if private else 0, name))

    def facts_to_scan(self):
        rows = self._con.execute(
            """SELECT f.id, s.name, f.statement, f.visibility, f.notes, f.trust_rationale, f.recheck_rationale,
                      f.source_quote, f.applies_to,
                      (SELECT group_concat(COALESCE(fs.locator, '') || ' ' || COALESCE(fs.quote, ''), char(10))
                       FROM fact_sources fs WHERE fs.fact_id = f.id)
               FROM facts f JOIN subjects s ON s.id = f.subject_id""").fetchall()
        return [FactText(fact_id, subject, statement, stored, tuple(extra))
                for fact_id, subject, statement, stored, *extra in rows]

    def raise_to_private(self, fact_id):
        self._con.execute("UPDATE facts SET visibility = 'private' WHERE id = ?", (fact_id,))
