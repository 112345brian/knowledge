"""`knowledge.py build | clean-concerts`: the commands that run a script (through `ctx.obj.scripts`)."""

import sys

import typer

app = typer.Typer(pretty_exceptions_enable=False, rich_markup_mode=None)


@app.command("build", help="Rebuild knowledge.db from schema.sql + scripts + data/.")
def cmd_build(ctx: typer.Context, check: bool = typer.Option(False, "--check", help="Build into a throwaway file and report counts; live DB untouched.")):
    c = ctx.obj
    try:
        code = c.scripts.build(check=check)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        raise typer.Exit(1)
    raise typer.Exit(code)


@app.command("clean-concerts", help="Clean concerts.csv in place (dedupes rows).")
def cmd_clean_concerts(ctx: typer.Context):
    c = ctx.obj
    try:
        code = c.scripts.clean_concerts()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        raise typer.Exit(1)
    raise typer.Exit(code)
