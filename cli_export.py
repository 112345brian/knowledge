"""CLI ring (tach layer `cli`): `knowledge.py db export | verify | import`: the database's deterministic text export (CLI-first rule #34).

`export` writes one file per curated table plus a manifest into a directory (default: `export/` in the private data
directory); `verify` fails when the files and the database disagree; `import` builds a NEW database from an export and
never overwrites one. The format is `export_rules`; the work is `export_service`.
"""

__arbor__ = {"ring": "cli"}

import sys
from typing import Optional

import typer

import export_rules

app = typer.Typer(help="Export the database to text files, verify the export, or import it into a new database.",
                  pretty_exceptions_enable=False, rich_markup_mode=None)

DIR_HELP = "Export directory (default: export/ in the private data directory)."


def _fail(message: str):
    print(f"error: {message}", file=sys.stderr)
    raise typer.Exit(1)


@app.command("export", help="Write one deterministic NDJSON file per curated table and a manifest.")
def cmd_export(ctx: typer.Context, directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP),
               db: Optional[str] = typer.Option(None, "--db", help="Database to export (default: the configured one).")):
    """Write the export files and print the row count of each table."""
    try:
        counts = ctx.obj.db_export(db, directory).export_database()
    except export_rules.ExportError as e:
        _fail(str(e))
    for table, rows in counts.items():
        print(f"{table}: {rows}")


@app.command("verify", help="Exit 1 when the export files differ from the database; writes nothing.")
def cmd_verify(ctx: typer.Context, directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP),
               db: Optional[str] = typer.Option(None, "--db", help="Database to compare (default: the configured one).")):
    """Compare the export files with the database and fail on any difference."""
    try:
        problems = ctx.obj.db_export(db, directory).verify_export()
    except export_rules.ExportError as e:
        _fail(str(e))
    if problems:
        print("the export and the database disagree:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        raise typer.Exit(1)
    print("the export matches the database")


@app.command("import", help="Create a NEW database from an export. Refuses to overwrite an existing file.")
def cmd_import(ctx: typer.Context, into: str = typer.Option(..., "--into", help="Path of the new database file."),
               directory: Optional[str] = typer.Option(None, "--dir", help=DIR_HELP)):
    """Create a new database from an export and print the row count of each table."""
    try:
        counts = ctx.obj.db_import(into, directory).import_database()
    except (export_rules.ExportError, FileExistsError) as e:
        _fail(str(e))
    for table, rows in counts.items():
        print(f"{table}: {rows}")
