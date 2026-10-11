"""SQLite adapter for `sources_port.SourceRepository`: the sources, publishers, authors, identifiers, relations
and vault_files tables of the build database, over the connection a step is given."""
import sqlite3
from typing import Dict, List, Mapping, Optional, Sequence, Set

from source_record import IdentifierConflict, SourceRecord
from source_status import Relation

_INSERT_SOURCE = """INSERT INTO sources (citekey, name, source_type, publisher_id, url, published_date, retrieved_date,
                            description, origin_path, content_sha256, size_bytes, file_mtime, mime_type, file_state,
                            status, status_date, status_note, edition, original_published_date,
                            acquired_at, acquired_via, where_from, acquired_note)
       VALUES (:citekey, :name, :source_type, :publisher_id, :url, :published_date, :retrieved_date,
               :description, :origin_path, :content_sha256, :size_bytes, :file_mtime, :mime_type, :file_state,
               :status, :status_date, :status_note, :edition, :original_published_date,
               :acquired_at, :acquired_via, :where_from, :acquired_note)"""


class SqliteSourceRepository:
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    # -- dimension tables
    def _get_or_create(self, table: str, name: str) -> int:
        row = self._con.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
        if row:
            return row[0]
        return self._con.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid

    # -- sources
    def citekeys(self) -> Set[str]:
        return {r[0] for r in self._con.execute("SELECT citekey FROM sources WHERE citekey IS NOT NULL")}

    def add_source(self, record: SourceRecord) -> List[IdentifierConflict]:
        con = self._con
        publisher_id = self._get_or_create("publishers", record.publisher) if record.publisher else None
        row = {
            "citekey": record.citekey, "name": record.name, "source_type": record.source_type, "publisher_id": publisher_id,
            "url": record.url, "published_date": record.published_date, "retrieved_date": record.retrieved_date,
            "description": record.description, "origin_path": record.origin_path,
            **{c: None for c in ("content_sha256", "size_bytes", "file_mtime", "mime_type", "file_state")},
            **(record.fingerprint or {}), **record.state.columns(), **record.acquisition,
        }
        source_id = con.execute(_INSERT_SOURCE, row).lastrowid
        conflicts = self._add_identifiers(source_id, record.identifiers)
        for order, name in record.authors:
            author_id = self._get_or_create("authors", name)
            con.execute("INSERT OR IGNORE INTO source_authors (source_id, author_id, author_order) VALUES (?, ?, ?)",
                        (source_id, author_id, order))
        return conflicts

    def _add_identifiers(self, source_id, pairs) -> List[IdentifierConflict]:
        con, conflicts = self._con, []
        for scheme, value in pairs:
            owner = con.execute("SELECT si.source_id, s.citekey FROM source_identifiers si JOIN sources s ON s.id = si.source_id "
                                "WHERE si.scheme = ? AND si.value = ?", (scheme, value)).fetchone()
            if owner is None:
                con.execute("INSERT INTO source_identifiers (source_id, scheme, value) VALUES (?, ?, ?)", (source_id, scheme, value))
            elif owner[0] != source_id:
                con.execute("INSERT OR IGNORE INTO source_identifier_conflicts (source_id, scheme, value, owner_source_id) "
                            "VALUES (?, ?, ?, ?)", (source_id, scheme, value, owner[0]))
                conflicts.append(IdentifierConflict(scheme, value, owner[1]))
        return conflicts

    # -- relations
    def add_relations(self, relations: Sequence[Relation]) -> None:
        ids = {citekey: sid for citekey, sid in self._con.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}
        for a, rel, b in relations:
            self._con.execute("INSERT INTO source_relations (source_id, relation, related_source_id) VALUES (?, ?, ?)",
                              (ids[a], rel, ids[b]))

    def status_counts(self) -> Dict[str, int]:
        return dict(self._con.execute("SELECT status, COUNT(*) FROM sources GROUP BY status").fetchall())

    # -- vault files
    def recorded_vault_file(self, path: str) -> Optional[int]:
        row = self._con.execute("SELECT id, file_state FROM vault_files WHERE path = ?", (path,)).fetchone()
        return row[0] if row is not None and row[1] is not None else None

    def record_vault_file(self, path: str, fingerprint: Mapping[str, object], acquisition: Mapping[str, Optional[str]]) -> int:
        file_id = self._vault_file_id(path)
        self._con.execute(
            "UPDATE vault_files SET content_sha256 = :content_sha256, size_bytes = :size_bytes, "
            "file_mtime = :file_mtime, mime_type = :mime_type, file_state = :file_state, "
            "acquired_at = :acquired_at, acquired_via = :acquired_via, where_from = :where_from, "
            "acquired_note = :acquired_note WHERE id = :id",
            {**fingerprint, **acquisition, "id": file_id})
        return file_id

    def _vault_file_id(self, path: str) -> int:
        row = self._con.execute("SELECT id FROM vault_files WHERE path = ?", (path,)).fetchone()
        if row:
            return row[0]
        return self._con.execute("INSERT INTO vault_files (path) VALUES (?)", (path,)).lastrowid
