"""SQLite adapter for `build_repositories_port.SubjectRepository` over the build database's subjects tables.

It stores what it is told and decides nothing: which subject a name means, which alias may be claimed and what a
hierarchy entry defaults to are `identity_service` and `subject_rules`. The one translation it owns is the
database's refusal of an alias (the table's uniqueness and the schema's triggers), which becomes `AliasRejected`.
Each `add` allocates its row when called, in call order (the golden dump compares the ids). It never commits; the
step that owns the connection does."""
import sqlite3
from typing import Mapping, Optional, Sequence

import build_records as r
import subject_rules
import subjects_store
from build_repositories_port import AliasRejected


class SqliteSubjects:
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def find_by_name(self, name: str) -> Optional[r.SubjectId]:
        row = self._con.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
        return r.SubjectId(row[0]) if row else None

    def find_by_alias(self, alias: str) -> Optional[r.SubjectId]:
        row = self._con.execute("SELECT subject_id FROM subject_aliases WHERE alias = ?", (alias,)).fetchone()
        return r.SubjectId(row[0]) if row else None

    def ids_by_name(self) -> Mapping[str, r.SubjectId]:
        return {name: r.SubjectId(i) for i, name in self._con.execute("SELECT id, name FROM subjects")}

    def add(self, name: str, domain: Optional[str] = None) -> r.SubjectId:
        if domain is None:
            cur = self._con.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
        else:
            cur = self._con.execute("INSERT INTO subjects (name, domain) VALUES (?, ?)", (name, domain))
        return r.SubjectId(cur.lastrowid)

    def ensure(self, name: str, domain: str) -> None:
        self._con.execute("INSERT OR IGNORE INTO subjects (name, domain) VALUES (?, ?)", (name, domain))

    def set_hierarchy(self, subject_id: r.SubjectId, parent_id: Optional[r.SubjectId], relation: str,
                      description: Optional[str], deprecated: bool) -> None:
        self._con.execute(
            "UPDATE subjects SET parent_id = ?, parent_relation = ?, description = ?, deprecated = ? WHERE id = ?",
            (parent_id, relation, description, 1 if deprecated else 0, subject_id))

    def set_replacement(self, subject_id: r.SubjectId, replaced_by_id: r.SubjectId) -> None:
        self._con.execute("UPDATE subjects SET replaced_by_subject_id = ? WHERE id = ?", (replaced_by_id, subject_id))

    def add_alias(self, subject_id: r.SubjectId, alias: str) -> None:
        try:
            self._con.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (?, ?)", (subject_id, alias))
        except sqlite3.IntegrityError as err:
            raise AliasRejected(str(err)) from None

    def count_with_parent(self) -> int:
        return self._count("SELECT COUNT(*) FROM subjects WHERE parent_id IS NOT NULL")

    def count_aliases(self) -> int:
        return self._count("SELECT COUNT(*) FROM subject_aliases")

    def count_deprecated(self) -> int:
        return self._count("SELECT COUNT(*) FROM subjects WHERE deprecated = 1")

    def read_file(self, path: str) -> Optional[Sequence[subject_rules.Subject]]:
        return subjects_store.read_file(path)

    def _count(self, sql: str) -> int:
        return self._con.execute(sql).fetchone()[0]
