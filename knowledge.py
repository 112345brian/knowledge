#!/usr/bin/env python3
"""Single entry point for this repo (Typer CLI; run `uv sync` once, then `uv run python knowledge.py --help`).

    knowledge.py build [--check]
    knowledge.py add-fact "statement" --subject x --trust medium ...
    knowledge.py search "some terms" [--subject x] [--trust high] [--personal-only|--not-personal] [--status S] [--include-pending] [--limit N] [--json]
    knowledge.py show <fact_id> [--as-of DATE] [--json]
    knowledge.py history <fact_id|source_key> [--json]
    knowledge.py audit-claims [--json]
    knowledge.py audit-sources [--json]
    knowledge.py source ids CITEKEY [--json]
    knowledge.py build-info [--compare OTHER.db] [--json]
    knowledge.py audit-source-status [--json]
    knowledge.py subject list|show|alias|describe|deprecate ...  [--json]  (subjects.json; see cli_subjects.py)
    knowledge.py entity list|show|add|alias|tag|untag|migrate-keywords ...  [--json]  (entities.json; see cli_entities.py)
    knowledge.py privacy check "statement" --subject s [--requested normal|private] [--json]
    knowledge.py privacy rules [--json]
    knowledge.py privacy tag|untag <subject> / add-keyword|remove-keyword <word>  [--allow-dirty] [--dry-run]
    knowledge.py subjects [--include-pending] [--json]
    knowledge.py facts [--subject x] [--trust high] [--status active|pending|...] [--include-pending] [--personal-only|--not-personal] [--limit N] [--json]
    knowledge.py review-pending [--json]
    knowledge.py approve REF... | --all [--reason TEXT] [--allow-dirty] [--json]
    knowledge.py reject REF... --reason TEXT [--allow-dirty] [--json]

Convention (issue #34), for every later command: the library function comes
first (pure, importable, returns data, never prints or exits), the Typer
command second (a thin printer, with `--json` on every read command), the MCP
tool / inbox handler third. `cli_parity.py` registers each action's CLI
command and tests/test_cli_parity.py fails when a registered command is
missing or a future MCP tool / inbox action has no registry entry.

`build` and `add-fact` are thin dispatches to the existing standalone scripts, while client tools are
registered by the client's bootstrap -- those
still run fine on their own, without typer; this just gives one name to
remember. `search`, `show`, `subjects`, and `facts` query knowledge.db.
"""
import sys

import typer

import container


# The commands live in the cli_* modules, one concern each; this module assembles them under one app.

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False, rich_markup_mode=None)


@app.callback()
def _entry(ctx: typer.Context):
    """The one place a command line is turned into a wired application: every command reads `ctx.obj`."""
    ctx.obj = container.Container()


import cli_facts_batch; cli_facts_batch.register(app)  # `add-facts` (#32)
import cli_add_fact; cli_add_fact.register(app)  # `add-fact` (#7)


import cli_build; app.add_typer(cli_build.app)  # build and registered client tools
import cli_facts; app.add_typer(cli_facts.app)  # search, show, history, subjects, facts
import cli_audits; app.add_typer(cli_audits.app)  # audit-claims, build-info, audit-source-status, audit-sources
import cli_review; app.add_typer(cli_review.app)  # review-pending, approve, reject (#6)
import cli_privacy; app.add_typer(cli_privacy.app, name="privacy")  # privacy check|rules|tag|untag|add-keyword|remove-keyword (#31)
import cli_sources; app.add_typer(cli_sources.app, name="source")  # source ids (#48)
import cli_entities; app.add_typer(cli_entities.app, name="entity")  # entity list|show|add|alias|tag|untag|migrate-keywords (#42)
import cli_subjects; app.add_typer(cli_subjects.app, name="subject")  # subject list|show|alias|describe|deprecate (#43)
import cli_lifecycle; app.add_typer(cli_lifecycle.app)  # supersede, retract, set-visibility (#8, #23)


import cli_migrate; cli_migrate.register(app)  # migrate-memory (#29)


from cli_inbox import register as _register_inbox; _register_inbox(app)  # inbox, edit (#33)

import cli_docs; app.add_typer(cli_docs.app, name="docs")  # docs build|check


def main(argv=None):
    """Run the CLI and return the exit code instead of exiting (used by tests)."""
    try:
        # click would otherwise glob-expand `*` and `~` in arguments on Windows (cmd has no globbing), which corrupts
        # a search term like `protei*`; the same command line must mean the same thing on every OS.
        app(args=argv, prog_name="knowledge.py", windows_expand_args=False)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
