"""CLI ring (tach layer `cli`): `knowledge.py db export | verify | import`, the database's deterministic text export (CLI-first rule #34).

`export` writes the backup folder (default: `export/` in the private data directory): `core/` (the curated tables and
the logs), `bulk/` (the client tables: music, measurements, ...) and, only with `--include-notes`, `notes/` (the
citation-note markdown). What is exported is a selection: `--table` and `--group` (repeatable) name tables or groups,
`--skip` removes tables, and naming nothing means everything the database has. `verify` takes the same selection and
fails when the folder and the database disagree; `import` builds a NEW database from the folder and never overwrites one. The format is `export_rules`; the work is `export_service` and `import_service`.
"""

__arbor__ = {"ring": "cli"}

import sys
from typing import List, Optional

import typer

import export_rules

app = typer.Typer(help="Export the database to a backup folder, verify the folder, or import it into a new database.",
                  pretty_exceptions_enable=False, rich_markup_mode=None)

DIR_HELP = "Export folder (default: export/ in the private data directory)."
DB_HELP = "Database (default: the configured one)."
TABLE_HELP = "A table to export (repeatable). With no --table or --group, everything the database has is selected."
GROUP_HELP = "A named group of tables (repeatable): facts, sources, subjects, entities, claims, logs, or a client source such as scrobbles."
SKIP_HELP = "A table to leave out (repeatable)."
NOTES_HELP = "Also handle the notes part: the citation-note markdown files of the notes vault."


def _fail(message: str):
    print(f"error: {message}", file=sys.stderr)
    raise typer.Exit(1)


def _print(result):
    for part, items in result.items():
        if part == export_rules.NOTES:
            print(f"{part}: {len(items)} file(s)")
            continue
        for table, rows in items.items():
            print(f"{part}/{table}: {rows}")


SELECTION = dict(
    table=typer.Option(None, "--table", help=TABLE_HELP),
    group=typer.Option(None, "--group", help=GROUP_HELP),
    skip=typer.Option(None, "--skip", help=SKIP_HELP),
    include_notes=typer.Option(False, "--include-notes", help=NOTES_HELP),
)


@app.command("export", help="Write the selected tables (default: all) as deterministic files, each part with a manifest.")
def cmd_export(ctx: typer.Context, directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP),
               db: Optional[str] = typer.Option(None, "--db", help=DB_HELP),
               table: Optional[List[str]] = SELECTION["table"], group: Optional[List[str]] = SELECTION["group"],
               skip: Optional[List[str]] = SELECTION["skip"], include_notes: bool = SELECTION["include_notes"]):
    """Write the selected tables and print the row count of each."""
    try:
        _print(ctx.obj.db_export(db, directory).export_database(table or (), group or (), skip or (), include_notes))
    except export_rules.ExportError as e:
        _fail(str(e))


@app.command("verify", help="Exit 1 when the backup folder differs from the database for the selection; writes nothing.")
def cmd_verify(ctx: typer.Context, directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP),
               db: Optional[str] = typer.Option(None, "--db", help=DB_HELP),
               table: Optional[List[str]] = SELECTION["table"], group: Optional[List[str]] = SELECTION["group"],
               skip: Optional[List[str]] = SELECTION["skip"], include_notes: bool = SELECTION["include_notes"]):
    """Compare the selected tables with the database and fail on any difference."""
    try:
        problems = ctx.obj.db_export(db, directory).verify_export(table or (), group or (), skip or (), include_notes)
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
               core_only: bool = typer.Option(False, "--core-only", help="Skip the bulk part (the client tables)."),
               notes_into: Optional[str] = typer.Option(None, "--notes-into", help="Also copy the exported notes into this directory (never overwrites).")):
    """Create a new database from an export and print the row count of each table."""
    try:
        result = ctx.obj.db_import(into, directory, notes_into).import_database(not core_only, notes_into is not None)
    except (export_rules.ExportError, FileExistsError) as e:
        _fail(str(e))
    _print(result)
