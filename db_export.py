"""Composition root (tach layer `composition`) for exporting the database to text files and importing them into a new database.

Binds `export_service` and `import_service` to the SQLite and directory adapters, and the client registry (through
`RegistryScope`) to the scope port.
"""

__arbor__ = {"ring": "composition"}

import client_bootstrap
import export_service
import import_service
from export_service import ExportDeps
from export_store import DirExportFiles, DirNotes, RegistryScope, SqliteExportReads, SqliteImportTarget
from import_service import ImportDeps
from wiring import bind_all

EXPORT_API = ("export_database", "verify_export")
IMPORT_API = ("import_database",)


def compose_export(db_path, directory, notes_dir=None, scope=None):
    """The export use cases for the database at `db_path`, the export directory `directory` and the notes directory
    `notes_dir` (needed only for the notes part)."""
    return bind_all(export_service, ExportDeps(SqliteExportReads(db_path), DirExportFiles(directory),
                                               scope or RegistryScope(client_bootstrap.registry()), DirNotes(notes_dir)), EXPORT_API)


def compose_import(db_path, directory, notes_into=None, scope=None):
    """The import use case: read the export in `directory`, create the new database at `db_path`; notes (if asked
    for) are copied into `notes_into`."""
    return bind_all(import_service, ImportDeps(DirExportFiles(directory), SqliteImportTarget(db_path),
                                               scope or RegistryScope(client_bootstrap.registry()), DirNotes(notes_into)), IMPORT_API)
