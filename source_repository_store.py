"""SQLite adapter for `build_repositories_port.SourceRepository`: the sources, publishers, authors, identifiers,
relations and vault_files tables of the build database, over the connection a step is given.

It stores what it is told and decides nothing: which identifier belongs to which source, and whether a vault file
needs its fixity recorded, are `identity_service`'s. Each `add` allocates its row when called, in call order (the
golden dump compares the ids)."""
import sqlite3
from typing import Mapping, Optional

import build_records as r

_SOURCE_COLUMNS = ("citekey", "name", "source_type", "publisher_id", "url", "published_date", "retrieved_date",
                   "description", "origin_path", "content_sha256", "size_bytes", "file_mtime", "mime_type", "file_state",
                   "status", "status_date", "status_note", "edition", "original_published_date",
                   "acquired_at", "acquired_via", "where_from", "acquired_note")
_INSERT_SOURCE = (f"INSERT INTO sources ({', '.join(_SOURCE_COLUMNS)}) "
                  f"VALUES ({', '.join(':' + c for c in _SOURCE_COLUMNS)})")
_FIXITY_COLUMNS = ("content_sha256", "size_bytes", "file_mtime", "mime_type", "file_state",
                   "acquired_at", "acquired_via", "where_from", "acquired_note")


class SqliteSourceRepository:
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def _get_or_create(self, table: str, name: str) -> int:
        row = self._con.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
        if row:
            return row[0]
        return self._con.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid

    # -- sources
    def citekeys(self) -> Mapping[str, r.SourceId]:
        return {citekey: r.SourceId(source_id) for citekey, source_id in
                self._con.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}

    def find(self, citekey: str) -> Optional[r.SourceId]:
        row = self._con.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
        return r.SourceId(row[0]) if row else None

    def add(self, source: r.NewSource) -> r.SourceId:
        publisher_id = self._get_or_create("publishers", source.publisher) if source.publisher else None
        row = {c: getattr(source, c, None) for c in _SOURCE_COLUMNS}
        row["publisher_id"] = publisher_id
        return r.SourceId(self._con.execute(_INSERT_SOURCE, row).lastrowid)

    # -- identifiers
    def identifier_owner(self, scheme: str, value: str) -> Optional[r.IdentifierOwner]:
        row = self._con.execute("SELECT si.source_id, s.citekey FROM source_identifiers si "
                                "JOIN sources s ON s.id = si.source_id WHERE si.scheme = ? AND si.value = ?",
                                (scheme, value)).fetchone()
        return r.IdentifierOwner(r.SourceId(row[0]), row[1]) if row else None

    def add_identifier(self, source_id: r.SourceId, scheme: str, value: str) -> None:
        self._con.execute("INSERT INTO source_identifiers (source_id, scheme, value) VALUES (?, ?, ?)",
                          (source_id, scheme, value))

    def record_identifier_conflict(self, source_id: r.SourceId, scheme: str, value: str,
                                   owner_source_id: r.SourceId) -> None:
        self._con.execute("INSERT OR IGNORE INTO source_identifier_conflicts (source_id, scheme, value, owner_source_id) "
                          "VALUES (?, ?, ?, ?)", (source_id, scheme, value, owner_source_id))

    # -- authors
    def link_author(self, source_id: r.SourceId, name: str, order: int = 0) -> None:
        author_id = self._get_or_create("authors", name)
        self._con.execute("INSERT OR IGNORE INTO source_authors (source_id, author_id, author_order) VALUES (?, ?, ?)",
                          (source_id, author_id, order))

    # -- vault files
    def vault_file(self, path: str) -> Optional[r.VaultFileState]:
        row = self._con.execute("SELECT id, file_state FROM vault_files WHERE path = ?", (path,)).fetchone()
        return r.VaultFileState(r.VaultFileId(row[0]), row[1]) if row else None

    def add_vault_file(self, path: str) -> r.VaultFileId:
        return r.VaultFileId(self._con.execute("INSERT INTO vault_files (path) VALUES (?)", (path,)).lastrowid)

    def record_vault_file_fixity(self, file_id: r.VaultFileId, fixity: r.FileFixity) -> None:
        values = {c: getattr(fixity, c) for c in _FIXITY_COLUMNS}
        self._con.execute("UPDATE vault_files SET " + ", ".join(f"{c} = :{c}" for c in _FIXITY_COLUMNS) +
                          " WHERE id = :id", {**values, "id": file_id})

    # -- relations and counts
    def add_relation(self, relation: r.NewSourceRelation) -> None:
        self._con.execute("INSERT INTO source_relations (source_id, relation, related_source_id) VALUES (?, ?, ?)",
                          (relation.source_id, relation.relation, relation.related_source_id))

    def count_by_status(self) -> Mapping[str, int]:
        return dict(self._con.execute("SELECT status, COUNT(*) FROM sources GROUP BY status").fetchall())
