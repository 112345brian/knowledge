"""CLI ring (tach layer `cli`): `knowledge.py db export | verify | import`, the database's deterministic text export (CLI-first rule #34).

`export` writes the backup folder (default: `export/` in the private data directory): `core/` (the curated tables),
`bulk/` (the client tables: music, measurements, ...) and, only with `--include-notes`, `notes/` (the citation-note
markdown). `verify` fails when the folder and the database disagree; `import` builds a NEW database from the folder
and never overwrites one. The format is `export_rules`; the work is `export_service` and `import_service`.
"""

__arbor__ = {"ring": "cli"}

import sys
from typing import Optional

import typer

import export_rules

app = typer.Typer(help="Export the database to a backup folder, verify the folder, or import it into a new database.",
                  pretty_exceptions_enable=False, rich_markup_mode=None)

DIR_HELP = "Export folder (default: export/ in the private data directory)."
DB_HELP = "Database (default: the configured one)."
PARTS_HELP = "Comma-separated parts of the export: core, bulk (default: core,bulk). Notes are added with --include-notes."
NOTES_HELP = "Also handle the notes part: the citation-note markdown files of the notes vault."


def _fail(message: str):
    print(f"error: {message}", file=sys.stderr)
    raise typer.Exit(1)


def _parts(parts: str, include_notes: bool):
    chosen = [p.strip() for p in parts.split(",") if p.strip()]
    if include_notes and export_rules.NOTES not in chosen:
        chosen.append(export_rules.NOTES)
    return tuple(chosen)


def _print(result):
    for part, items in result.items():
        if part == export_rules.NOTES:
            print(f"{part}: {len(items)} file(s)")
            continue
        for table, rows in items.items():
            print(f"{part}/{table}: {rows}")


@app.command("export", help="Write the backup folder: core and bulk table files (and notes with --include-notes), each part with a manifest.")
def cmd_export(ctx: typer.Context, directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP),
               db: Optional[str] = typer.Option(None, "--db", help=DB_HELP),
               parts: str = typer.Option("core,bulk", "--parts", help=PARTS_HELP),
               include_notes: bool = typer.Option(False, "--include-notes", help=NOTES_HELP)):
    """Write the export parts and print the row count of each table."""
    try:
        _print(ctx.obj.db_export(db, directory).export_database(_parts(parts, include_notes)))
    except export_rules.ExportError as e:
        _fail(str(e))


@app.command("verify", help="Exit 1 when the backup folder differs from the database; writes nothing.")
def cmd_verify(ctx: typer.Context, directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP),
               db: Optional[str] = typer.Option(None, "--db", help=DB_HELP),
               parts: str = typer.Option("core,bulk", "--parts", help=PARTS_HELP),
               include_notes: bool = typer.Option(False, "--include-notes", help=NOTES_HELP)):
    """Compare the export parts with the database and fail on any difference."""
    try:
        problems = ctx.obj.db_export(db, directory).verify_export(_parts(parts, include_notes))
    except export_rules.ExportError as e:
        _fail(str(e))
    if problems:
        print("the export and the database disagree:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        raise typer.Exit(1)
    print("the export matches the database")


@app.command("import", help="Create a NEW database from the backup folder. Refuses to overwrite an existing file or note.")
def cmd_import(ctx: typer.Context, into: str = typer.Option(..., "--into", help="Path of the new database file."),
               directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP),
               core_only: bool = typer.Option(False, "--core-only", help="Import only the core tables, not the bulk client tables."),
               notes_into: Optional[str] = typer.Option(None, "--notes-into", help="Also copy the exported notes into this directory (never overwrites).")):
    """Create a new database from an export and print the row count of each table."""
    try:
        result = ctx.obj.db_import(into, directory, notes_into).import_database(not core_only, notes_into is not None)
    except (export_rules.ExportError, FileExistsError) as e:
        _fail(str(e))
    _print(result)
