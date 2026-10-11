"""Typer commands for the generated docs (CLI-first rule #34): `docs build` rewrites them, `docs check` fails when stale.

The README command table and the CLI reference are rendered from this very command tree, so the commands are
read off the click group the app turns into (`command_docs`), not parsed from `--help` output. The rendering rules
are `docs_rules`; the files are touched by `docs` through its port.
"""
import sys
from typing import Optional

import typer

import docs_rules

app = typer.Typer(help="Regenerate or verify the generated regions of README.md and docs/*.md.",
                  pretty_exceptions_enable=False, rich_markup_mode=None)


def _argument(param):
    name = param.metavar or param.name.upper()
    return docs_rules.ArgumentDoc(name=name, required=bool(param.required), many=param.nargs == -1)


def _option(param):
    names = tuple(sorted(param.opts)) + tuple(sorted(param.secondary_opts))
    value = "" if getattr(param, "is_flag", False) else (param.metavar or param.name.upper())
    return docs_rules.OptionDoc(names=names, value=value, required=bool(param.required), help=getattr(param, "help", "") or "")


def command_docs(group, prefix=()):
    """Plain `CommandDoc` values for every visible command under a click group, nested groups included."""
    found = []
    for name in sorted(group.commands):
        cmd = group.commands[name]
        if getattr(cmd, "hidden", False):
            continue
        path = (*prefix, name)
        if hasattr(cmd, "commands"):
            found.extend(command_docs(cmd, path))
            continue
        params = [p for p in cmd.params if not getattr(p, "hidden", False)]
        found.append(docs_rules.CommandDoc(
            path=path,
            help=cmd.help or "",
            arguments=tuple(_argument(p) for p in params if p.param_type_name == "argument"),
            options=tuple(_option(p) for p in params if p.param_type_name == "option"),
        ))
    return found


def _generator(ctx: typer.Context, root: Optional[str]):
    container = ctx.obj
    return container.docs_at(root), command_docs(ctx.find_root().command)


ROOT_HELP = "Repository root to read and write (default: this checkout)."


@app.command("build", help="Rewrite every generated region of README.md and docs/*.md from the code. Hand-written text is untouched.")
def cmd_build(ctx: typer.Context, root: Optional[str] = typer.Option(None, "--root", help=ROOT_HELP)):
    generator, commands = _generator(ctx, root)
    try:
        changed = generator.build(commands)
    except docs_rules.DocsError as e:
        print(f"error: {e}", file=sys.stderr)
        raise typer.Exit(1)
    for path in changed:
        print(f"updated {path}")
    if not changed:
        print("generated docs are already current")


@app.command("check", help="Fail (exit 1) when a generated region differs from what the code renders; writes nothing.")
def cmd_check(ctx: typer.Context, root: Optional[str] = typer.Option(None, "--root", help=ROOT_HELP)):
    generator, commands = _generator(ctx, root)
    try:
        stale = generator.check(commands)
    except docs_rules.DocsError as e:
        print(f"error: {e}", file=sys.stderr)
        raise typer.Exit(1)
    if stale:
        print("generated docs are stale (run `uv run python knowledge.py docs build` and commit):", file=sys.stderr)
        for line in stale:
            print(f"  {line}", file=sys.stderr)
        raise typer.Exit(1)
    print("generated docs are current")
