"""CLI ring (tach layer `cli`): `knowledge.py db snapshot | snapshots | restore | restore-tables` (CLI-first rule #34).

`snapshot` takes a verified, consistent copy of the database (SQLite's backup API, never a plain file copy) into the
snapshot directory (default: `snapshots/` beside the database) and keeps the newest N. `restore` copies a snapshot to
a NEW file and never overwrites; to replace the live database, move the old one away yourself and restore into its
place. `restore-tables` replaces chosen tables INSIDE an existing database from a snapshot file or an export folder, in
one transaction, after a safety snapshot, and refuses (changing nothing) if that would leave dangling references.
"""

__arbor__ = {"ring": "cli"}

import sys
from typing import List, Optional

import typer

import snapshot_rules
import export_rules
from cli_export import DB_HELP, fail

SNAP_DIR_HELP = "Snapshot directory (default: snapshots/ beside the database)."


def cmd_snapshot(ctx: typer.Context, db: Optional[str] = typer.Option(None, "--db", help=DB_HELP),
                 directory: Optional[str] = typer.Option(None, "--dir", help=SNAP_DIR_HELP),
                 keep: int = typer.Option(snapshot_rules.DEFAULT_KEEP, "--keep", help="How many snapshots to keep.")):
    """Take a snapshot and print its path (and any pruned or warned about)."""
    try:
        result = ctx.obj.db_snapshots(db, directory).take_snapshot(keep)
    except (snapshot_rules.SnapshotError, OSError) as e:
        fail(str(e))
    print(result.path)
    for name in result.pruned:
        print(f"pruned {name}")
    if result.warning:
        print(f"warning: {result.warning}", file=sys.stderr)


def cmd_snapshots(ctx: typer.Context, db: Optional[str] = typer.Option(None, "--db", help=DB_HELP),
                  directory: Optional[str] = typer.Option(None, "--dir", help=SNAP_DIR_HELP)):
    """Print the snapshot names, newest first."""
    for name in ctx.obj.db_snapshots(db, directory).list_snapshots():
        print(name)


def cmd_restore(ctx: typer.Context, source: str = typer.Option(..., "--from", help="The snapshot (or any database file) to copy."),
                into: str = typer.Option(..., "--into", help="Path of the new database file.")):
    """Verify the snapshot and copy it to a new file."""
    try:
        version = ctx.obj.db_snapshots().restore_snapshot(source, into)
    except (snapshot_rules.SnapshotError, OSError) as e:
        fail(str(e))
    print(f"restored {source} to {into} (schema version {version})")


def cmd_restore_tables(ctx: typer.Context, table: List[str] = typer.Option(..., "--table", help="A table to replace (repeatable)."),
                       from_snapshot: Optional[str] = typer.Option(None, "--from-snapshot", help="Take the rows from this snapshot file."),
                       from_export: Optional[str] = typer.Option(None, "--from-export", help="Take the rows from this export folder."),
                       db: Optional[str] = typer.Option(None, "--db", help=DB_HELP),
                       snapshot_dir: Optional[str] = typer.Option(None, "--snapshot-dir", help=SNAP_DIR_HELP),
                       dry_run: bool = typer.Option(False, "--dry-run", help="Do everything, then roll back and change nothing.")):
    """Replace the tables and print how many rows each got and where the safety snapshot is."""
    if (from_snapshot is None) == (from_export is None):
        fail("name exactly one source: --from-snapshot FILE or --from-export DIR")
    try:
        api = ctx.obj.db_restore_tables(db, from_snapshot, from_export, snapshot_dir)
        result = (api.restore_tables_from_snapshot if from_snapshot else api.restore_tables_from_export)(table, dry_run)
    except (snapshot_rules.SnapshotError, export_rules.ExportError, OSError) as e:
        fail(str(e))
    for name, rows in result.replaced.items():
        print(f"{name}: {rows}")
    print("dry run: nothing changed" if not result.committed else f"safety snapshot: {result.safety_snapshot}")


def register(db_app: typer.Typer) -> None:
    """Add the snapshot commands to the `db` group."""
    db_app.command("snapshot", help="Take a verified snapshot of the database and keep only the newest ones.")(cmd_snapshot)
    db_app.command("snapshots", help="List the snapshots, newest first.")(cmd_snapshots)
    db_app.command("restore", help="Copy a snapshot to a NEW database file. Never overwrites.")(cmd_restore)
    db_app.command("restore-tables", help="Replace chosen tables inside an existing database from a snapshot or an export folder, in one transaction.")(cmd_restore_tables)
