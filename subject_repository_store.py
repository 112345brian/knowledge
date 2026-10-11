"""Adapter for `subject_repository_port.SubjectRepository` over the build database's subjects tables."""
import sqlite3

from subject_rules import AliasRefused, HierarchyTally


class SqliteSubjects:
    """The subject repository on one connection. It never commits; the step that owns the connection does."""

    def __init__(self, con):
        self._con = con

    def find_subject(self, name):
        row = self._con.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def find_alias(self, alias):
        row = self._con.execute("SELECT subject_id FROM subject_aliases WHERE alias = ?", (alias,)).fetchone()
        return row[0] if row else None

    def subject_ids(self):
        return {name: i for i, name in self._con.execute("SELECT id, name FROM subjects")}

    def alias_owners(self):
        return {alias: name for alias, name in self._con.execute(
            "SELECT a.alias, s.name FROM subject_aliases a JOIN subjects s ON s.id = a.subject_id")}

    def create_subject(self, name, domain=None):
        if domain is None:
            cur = self._con.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
        else:
            cur = self._con.execute("INSERT INTO subjects (name, domain) VALUES (?, ?)", (name, domain))
        return cur.lastrowid

    def ensure_subject(self, name, domain):
        self._con.execute("INSERT OR IGNORE INTO subjects (name, domain) VALUES (?, ?)", (name, domain))

    def place(self, subject_id, parent_id, relation, description, deprecated):
        self._con.execute(
            "UPDATE subjects SET parent_id = ?, parent_relation = ?, description = ?, deprecated = ? WHERE id = ?",
            (parent_id, relation, description, 1 if deprecated else 0, subject_id))

    def replace_with(self, subject_id, replacement_id):
        self._con.execute("UPDATE subjects SET replaced_by_subject_id = ? WHERE id = ?", (replacement_id, subject_id))

    def add_alias(self, subject_id, alias):
        try:
            self._con.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (?, ?)", (subject_id, alias))
        except sqlite3.IntegrityError as err:
            raise AliasRefused(str(err)) from None

    def tally(self):
        return HierarchyTally(self._count("SELECT COUNT(*) FROM subjects WHERE parent_id IS NOT NULL"),
                              self._count("SELECT COUNT(*) FROM subject_aliases"),
                              self._count("SELECT COUNT(*) FROM subjects WHERE deprecated = 1"))

    def _count(self, sql):
        return self._con.execute(sql).fetchone()[0]
