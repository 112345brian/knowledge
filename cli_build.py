"""`knowledge.py build` and registered client tools: commands that run a script (through `ctx.obj.scripts`)."""

import sys

import typer

import client_bootstrap

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


def _register_tool(tool):
    @app.command(tool.command, help=tool.help or f"Run {tool.module}.")
    def run(ctx: typer.Context):
        c = ctx.obj
        try:
            code = c.scripts.run_tool(tool.module)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            raise typer.Exit(1)
        raise typer.Exit(code)


# The tools the client's bootstrap registers become commands; nothing here names one.
for _tool in client_bootstrap.registry().tools():
    _register_tool(_tool)
