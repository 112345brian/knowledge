"""Adapters of the database export and import: a SQLite file read-only, a directory of text files, a new SQLite file.

Infrastructure ring (tach layer `infrastructure`): implements `export_port`. Reading opens the database read-only so
an export can never change it; writing a file is atomic (a temp file in the same directory, then one replace);
creating a database refuses to touch one that exists.
"""

__arbor__ = {"ring": "infrastructure"}

import os
import sqlite3
import tempfile
from typing import Any, Collection, Iterator, List, Mapping, Sequence

import audit_rules
from export_rules import ExportError

SCHEMA_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def _columns(con: sqlite3.Connection, table: str) -> List[tuple]:
    return con.execute(f"PRAGMA table_info({audit_rules.ident(table)})").fetchall()


class SqliteExportReads:
    """`ExportReads` over a database file, opened read-only."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        if not os.path.isfile(self.db_path):
            raise ExportError(f"there is no database at {self.db_path}")
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)

    def user_version(self) -> int:
        """The schema version of the database."""
        con = self._connect()
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()

    def tables(self) -> Collection[str]:
        """The names of the tables that exist."""
        con = self._connect()
        try:
            return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            con.close()

    def rows(self, table: str) -> Iterator[Mapping[str, Any]]:
        """Every row of `table`, in primary-key order."""
        con = self._connect()
        try:
            columns = _columns(con, table)
            names = [c[1] for c in columns]
            keys = [c[1] for c in sorted((c for c in columns if c[5]), key=lambda c: c[5])]
            if not keys:
                raise ExportError(f"{table} has no primary key, so its rows have no stable order")
            cursor = con.execute(f"SELECT {', '.join(names)} FROM {table} ORDER BY {', '.join(keys)}")
            for row in cursor:
                yield dict(zip(names, row))
        finally:
            con.close()


class DirExportFiles:
    """`ExportFiles` over a directory (created on the first write)."""

    def __init__(self, directory: str):
        self.directory = directory

    def read(self, name: str) -> str:
        """The text of file `name`."""
        with open(os.path.join(self.directory, name), encoding="utf-8", newline="") as f:
            return f.read()

    def write(self, name: str, text: str) -> None:
        """Write `text` as file `name` atomically."""
        os.makedirs(self.directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".export-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, os.path.join(self.directory, name))
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


class SqliteImportTarget:
    """`ImportTarget` over a new database file with the core schema. Foreign keys are checked at the end, not per row
    (the tables arrive parents first, but `facts.superseded_by_fact_id` points inside its own table)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._con = None

    def create(self) -> None:
        """Create the new database with the core schema; refuse to overwrite one."""
        if os.path.exists(self.db_path):
            raise FileExistsError(f"{self.db_path} already exists; an import never overwrites a database")
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._con = sqlite3.connect(self.db_path)
        try:
            self._con.execute("PRAGMA foreign_keys = OFF")
            with open(SCHEMA_SQL, encoding="utf-8") as f:
                self._con.executescript(f.read())
            self._con.execute("PRAGMA foreign_keys = OFF")
        except BaseException:
            self.discard()
            raise

    def user_version(self) -> int:
        """The schema version of the database."""
        return self._con.execute("PRAGMA user_version").fetchone()[0]

    def insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Insert `rows` into `table`; refuse a column this schema lacks."""
        known = {c[1] for c in _columns(self._con, table)}
        for row in rows:
            extra = set(row) - known
            if extra:
                raise ExportError(f"{table}: the export has column(s) this schema lacks: {', '.join(sorted(extra))}")
        for row in rows:
            names = list(row)
            self._con.execute(f"INSERT INTO {audit_rules.ident(table)} ({', '.join(audit_rules.ident(n) for n in names)}) "
                              f"VALUES ({', '.join('?' for _ in names)})", [row[n] for n in names])

    def finish(self) -> List[str]:
        """Commit and return the integrity and foreign-key problems found."""
        self._con.commit()
        problems = [f"integrity: {r[0]}" for r in self._con.execute("PRAGMA integrity_check") if r[0] != "ok"]
        problems += [f"foreign key: {r[0]} row {r[1]} -> {r[2]}" for r in self._con.execute("PRAGMA foreign_key_check")]
        self._con.close()
        self._con = None
        return problems

    def discard(self) -> None:
        """Close and remove the database and its journal files."""
        if self._con is not None:
            self._con.close()
            self._con = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.remove(self.db_path + suffix)
