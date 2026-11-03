"""Composition root (tach layer `composition`) for database snapshots and the in-place restore of chosen tables.

Binds `snapshot_service` and `restore_tables_service` to the SQLite adapters: `compose_snapshots` for taking, listing
and restoring snapshots of a database, `compose_restore_tables` for replacing chosen tables of a database from a
snapshot file or an export folder.
"""

__arbor__ = {"ring": "composition"}

import restore_tables_service
import snapshot_service
from export_store import DirExportFiles, SqliteExportReads
from restore_tables_service import RestoreTablesDeps
from snapshot_service import SnapshotDeps
from snapshot_store import SqliteSnapshots, SqliteTableRestore
from wiring import bind_all

SNAPSHOT_API = ("take_snapshot", "list_snapshots", "restore_snapshot")
RESTORE_TABLES_API = ("restore_tables_from_snapshot", "restore_tables_from_export")


def compose_snapshots(db_path, directory, clock):
    """Snapshot use cases for the database at `db_path`, with snapshots kept in `directory`."""
    return bind_all(snapshot_service, SnapshotDeps(SqliteSnapshots(db_path, directory), clock), SNAPSHOT_API)


def compose_restore_tables(db_path, snapshot_dir, clock, snapshot_source=None, export_dir=None):
    """Table-restore use cases for the database at `db_path`. `snapshot_source` (a database file) and `export_dir` (an
    export folder) are the two places rows can come from; the safety snapshot goes into `snapshot_dir`."""
    return bind_all(restore_tables_service, RestoreTablesDeps(
        SqliteTableRestore(db_path), SqliteSnapshots(db_path, snapshot_dir), SqliteExportReads(snapshot_source or ""),
        DirExportFiles(export_dir or ""), clock), RESTORE_TABLES_API)
