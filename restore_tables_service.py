"""Replace chosen tables inside an existing database from a snapshot or an export folder.

Application ring (tach layer `application`): the in-place counterpart of `import` (which only builds a new database).
Before anything changes it takes a verified snapshot of the database (unless this is a dry run), checks that the schema
versions agree, that the source holds every named table and, for an export, that its files match their manifest.
All the tables are then replaced in ONE transaction, and the transaction is rolled back, leaving the database as it was,
if the replacement would introduce a foreign-key problem the database did not already have. History tables are refused
(`snapshot_rules.HISTORY_TABLES`). It never prints or exits.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence

import export_rules
import snapshot_rules as rules
import snapshot_service
from clock_port import Clock
from export_port import ExportFiles, ExportReads
from snapshot_port import Snapshots, TableRestore

VIA = "restore"


@dataclass(frozen=True)
class RestoreTablesDeps:
    """What the table-restore use cases reach the world through."""
    table_restore: TableRestore
    snapshots: Snapshots
    snapshot_reads: ExportReads
    export_files: ExportFiles
    clock: Clock


@dataclass(frozen=True)
class RestoreResult:
    """Rows written per table, the safety snapshot taken first ('' for a dry run), and whether anything was committed."""
    replaced: Mapping[str, int]
    safety_snapshot: str
    committed: bool


def _apply(deps: RestoreTablesDeps, tables: List[str], rows: Mapping[str, Sequence[Mapping[str, Any]]], dry_run: bool) -> RestoreResult:
    missing = [t for t in tables if t not in deps.table_restore.tables()]
    if missing:
        raise rules.SnapshotError("the database lacks table(s): " + ", ".join(missing))
    safety = ""
    if not dry_run:
        safety = snapshot_service.take_snapshot(snapshot_service.SnapshotDeps(deps.snapshots, deps.clock)).path
    staged = deps.table_restore.stage(VIA, {t: rows[t] for t in tables})
    try:
        broken = staged.new_violations()
        if broken:
            raise rules.SnapshotError("restoring would leave dangling references: " + "; ".join(broken[:5])
                                      + (f" (and {len(broken) - 5} more)" if len(broken) > 5 else ""))
        if dry_run:
            staged.discard()
        else:
            staged.apply()
    except BaseException:
        staged.discard()
        raise
    return RestoreResult({t: len(rows[t]) for t in tables}, safety, not dry_run)


def restore_tables_from_snapshot(deps: RestoreTablesDeps, tables: Sequence[str], dry_run: bool = False) -> RestoreResult:
    """Replace `tables` with their rows from the snapshot database behind `snapshot_reads`."""
    names = rules.check_restorable(tables)
    version = deps.snapshot_reads.user_version()
    if version != deps.table_restore.user_version():
        raise rules.SnapshotError(f"the snapshot is schema version {version}, the database is {deps.table_restore.user_version()}")
    absent = [t for t in names if t not in deps.snapshot_reads.tables()]
    if absent:
        raise rules.SnapshotError("the snapshot lacks table(s): " + ", ".join(absent))
    return _apply(deps, names, {t: list(deps.snapshot_reads.rows(t)) for t in names}, dry_run)


def restore_tables_from_export(deps: RestoreTablesDeps, tables: Sequence[str], dry_run: bool = False) -> RestoreResult:
    """Replace `tables` with their rows from the export folder, after checking each file against its manifest."""
    names = rules.check_restorable(tables)
    rows: Dict[str, List[Mapping[str, Any]]] = {}
    for table in names:
        part = export_rules.part_of(table)
        manifest = export_rules.read_manifest(deps.export_files.read, part)
        if manifest.user_version != deps.table_restore.user_version():
            raise rules.SnapshotError(f"the export is schema version {manifest.user_version}, the database is "
                                      f"{deps.table_restore.user_version()}")
        if table not in manifest.tables:
            raise rules.SnapshotError(f"the export does not hold {table}")
        texts = export_rules.read_texts(deps.export_files.read, part, export_rules.Manifest(manifest.user_version, {table: manifest.tables[table]}))
        problems = export_rules.drift(export_rules.Manifest(manifest.user_version, {table: manifest.tables[table]}), texts)
        if problems:
            raise rules.SnapshotError("the export is damaged: " + "; ".join(problems))
        rows[table] = export_rules.decode_table(texts[table])
    return _apply(deps, names, rows, dry_run)
