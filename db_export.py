"""Composition root (tach layer `composition`) for exporting the database to text files and importing them into a new database.

Binds `export_service` to the SQLite and directory adapters: `compose_export(db_path, directory)` for `export_database`
and `verify_export`, `compose_import(db_path, directory)` for `import_database`.
"""

__arbor__ = {"ring": "composition"}

import export_service
import import_service
from export_service import ExportDeps
from import_service import ImportDeps
from export_store import DirExportFiles, SqliteExportReads, SqliteImportTarget
from wiring import bind_all

EXPORT_API = ("export_database", "verify_export")
IMPORT_API = ("import_database",)


def compose_export(db_path, directory):
    """The export use cases for the database at `db_path` and the export directory `directory`."""
    return bind_all(export_service, ExportDeps(SqliteExportReads(db_path), DirExportFiles(directory)), EXPORT_API)


def compose_import(db_path, directory):
    """The import use case: read the export in `directory`, create the new database at `db_path`."""
    return bind_all(import_service, ImportDeps(DirExportFiles(directory), SqliteImportTarget(db_path)), IMPORT_API)
