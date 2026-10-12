"""Typer commands about sources (#48): `source ids CITEKEY`.

A thin printer over `queries.source_ids` (identifiers.py holds the rules). Registered in knowledge.py with
`app.add_typer(cli_sources.app, name="source")`; it must not import knowledge at import time. Read-only.
"""
import json
import sys

import typer

from fact_reads_port import DatabaseNotFound, FactQueryError

app = typer.Typer(help="Sources: their persistent identifiers.", pretty_exceptions_enable=False, rich_markup_mode=None)

JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")


def _fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(1)


@app.command("ids", help="Show a source's identifiers (DOI, ISBN, ISSN, PMID, arXiv, other) and any it shares with another source.")
def cmd_ids(ctx: typer.Context, citekey: str, as_json: bool = JSON_OPT):
    c = ctx.obj
    try:
        info = c.queries.source_ids(citekey)
    except DatabaseNotFound as e:
        _fail(e)
    except FactQueryError as e:
        _fail(f"{e} (rebuild knowledge.db with the current schema)")
    if info is None:
        _fail(f"unknown source {citekey!r}")
    if as_json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return
    print(f"{info['citekey']}  {info['name']}")
    for i in info["identifiers"]:
        print(f"  {i['scheme']}: {i['value']}")
    if not info["identifiers"]:
        print("  (no identifiers recorded)")
    for c in info["conflicts"]:
        print(f"  shared, not recorded here: {c['scheme']}:{c['value']} belongs to {c['owner']}")
