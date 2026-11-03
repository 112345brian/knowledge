"""Adapters of snapshots and of the in-place table restore: SQLite's online backup API and one transaction.

Infrastructure ring (tach layer `infrastructure`): implements `snapshot_port`. A snapshot is taken with
`Connection.backup` (a consistent copy even while another connection writes, unlike copying the file), written to a
temporary name in the destination directory, checked with `PRAGMA integrity_check`, and only then linked under its
final name, which fails if that name exists: a half-written or overwritten snapshot cannot happen.
"""

__arbor__ = {"ring": "infrastructure"}

import os
import sqlite3
import tempfile
from typing import Any, List, Mapping, Optional, Sequence, Tuple

import audit_rules
import snapshot_rules
from snapshot_rules import SnapshotError


def _problems(con: sqlite3.Connection) -> List[str]:
    return [r[0] for r in con.execute("PRAGMA integrity_check") if r[0] != "ok"]


def _open_readonly(path: str) -> sqlite3.Connection:
    if not os.path.isfile(path):
        raise SnapshotError(f"there is no database at {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _backup_exclusive(source: str, dest: str) -> None:
    """Copy the database `source` to `dest` through the backup API, verified, atomically and never over an existing file."""
    if os.path.exists(dest):
        raise FileExistsError(f"{dest} already exists")
    src = _open_readonly(source)                   # first: a missing source must leave nothing behind
    tmp = ""
    try:
        directory = os.path.dirname(dest) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".snapshot-partial-")
        os.close(fd)
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
            problems = _problems(out)
        finally:
            out.close()
        if problems:
            raise SnapshotError("the copy failed its integrity check: " + "; ".join(problems))
        try:
            os.link(tmp, dest)                     # fails if `dest` appeared meanwhile: never overwrites
        except FileExistsError:
            raise
        except OSError:                            # a file system without hard links: a checked rename instead
            if os.path.exists(dest):
                raise FileExistsError(f"{dest} already exists")
            os.rename(tmp, dest)
    finally:
        src.close()
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


class SqliteSnapshots:
    """`Snapshots` for one live database and one snapshot directory."""

    def __init__(self, db_path: str, directory: str):
        self.db_path = db_path
        self.directory = directory

    def create(self, name: str) -> str:
        """Take a verified consistent copy of the live database as `name` in the directory; never over an existing file."""
        dest = os.path.join(self.directory, name)
        _backup_exclusive(self.db_path, dest)
        return dest

    def names(self) -> List[str]:
        """Every file name in the snapshot directory (none when it does not exist)."""
        return sorted(os.listdir(self.directory)) if os.path.isdir(self.directory) else []

    def remove(self, name: str) -> None:
        """Delete snapshot `name`."""
        os.remove(os.path.join(self.directory, name))

    def check(self, path: str) -> Tuple[int, List[str]]:
        """(schema version, integrity problems) of the database file at `path`."""
        con = _open_readonly(path)
        try:
            return con.execute("PRAGMA user_version").fetchone()[0], _problems(con)
        except sqlite3.DatabaseError as e:
            return 0, [str(e)]
        finally:
            con.close()

    def copy_new(self, source: str, dest: str) -> None:
        """Copy the verified database `source` to the new file `dest`; FileExistsError if `dest` exists."""
        _backup_exclusive(source, dest)


class _Staged:
    """A replacement applied inside an open transaction (see `SqliteTableRestore.stage`)."""

    def __init__(self, con: sqlite3.Connection, introduced: List[str]):
        self._con: Optional[sqlite3.Connection] = con
        self._introduced = introduced

    def new_violations(self) -> List[str]:
        """The foreign-key problems the replacement introduced."""
        return self._introduced

    def _end(self, sql: str) -> None:
        if self._con is None:
            return
        try:
            if self._con.in_transaction:
                self._con.execute(sql)
        finally:
            self._con.close()
            self._con = None

    def apply(self) -> None:
        """Clear the audit context and commit."""
        if self._con is not None and self._con.execute("SELECT 1 FROM sqlite_master WHERE name = 'audit_context'").fetchone():
            self._con.execute("DELETE FROM audit_context")
        self._end("COMMIT")

    def discard(self) -> None:
        """Roll the transaction back."""
        self._end("ROLLBACK")


class SqliteTableRestore:
    """`TableRestore` over one database file. Foreign keys are off for the connection (a restored table may point at a
    row that is replaced a moment later); `stage` compares the foreign-key problems before and after instead. The audit
    context (when the database has one) names the restore, so its row changes are attributed to it."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        if not os.path.isfile(self.db_path):
            raise SnapshotError(f"there is no database at {self.db_path}")
        con = sqlite3.connect(self.db_path, isolation_level=None)      # autocommit: BEGIN/COMMIT are explicit
        con.execute("PRAGMA foreign_keys = OFF")
        return con

    def user_version(self) -> int:
        """The schema version of the database."""
        con = self._connect()
        try:
            return con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()

    def tables(self) -> List[str]:
        """The tables that exist."""
        con = self._connect()
        try:
            return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        finally:
            con.close()

    @staticmethod
    def _violations(con: sqlite3.Connection) -> List[str]:
        return [f"{r[0]} row {r[1]} -> {r[2]}" for r in con.execute("PRAGMA foreign_key_check")]

    @staticmethod
    def _replace(con: sqlite3.Connection, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        name = audit_rules.ident(table)
        known = {r[1] for r in con.execute(f"PRAGMA table_info({name})")}
        for row in rows:
            extra = set(row) - known
            if extra:
                raise SnapshotError(f"{table}: the source has column(s) this database lacks: {', '.join(sorted(extra))}")
        con.execute(f"DELETE FROM {name}")
        for row in rows:
            cols = list(row)
            con.execute(f"INSERT INTO {name} ({', '.join(audit_rules.ident(c) for c in cols)}) "
                        f"VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols])

    def stage(self, via: str, replacements: Mapping[str, Sequence[Mapping[str, Any]]]) -> _Staged:
        """Replace the named tables inside one immediate transaction and return it, still open, for the caller to apply or
        discard. A failure rolls everything back before it propagates."""
        con = self._connect()
        try:
            has_context = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'audit_context'").fetchone()
            con.execute("BEGIN IMMEDIATE")
            if has_context:
                con.execute("INSERT OR REPLACE INTO audit_context (id, via, session_id, at) VALUES (1, ?, NULL, NULL)", (via,))
            before = self._violations(con)
            for table, rows in replacements.items():
                self._replace(con, table, rows)
            return _Staged(con, snapshot_rules.new_violations(before, self._violations(con)))
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.close()
            raise
