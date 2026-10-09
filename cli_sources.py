"""Typer commands about sources (#48): `source ids CITEKEY`.

A thin printer over the built db (identifiers.py holds the rules). Registered in knowledge.py with
`app.add_typer(cli_sources.app, name="source")`; it must not import knowledge at import time, so the db is opened
through a lazy import. Read-only.
"""
import json
import sys

import typer

app = typer.Typer(help="Sources: their persistent identifiers.", pretty_exceptions_enable=False, rich_markup_mode=None)

JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")


def _fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(1)


def source_ids(con, citekey):
    """{'citekey', 'name', 'identifiers': [{scheme, value}], 'conflicts': [{scheme, value, owner}]} or None for an unknown citekey."""
    row = con.execute("SELECT id, citekey, name FROM sources WHERE citekey = ?", (citekey,)).fetchone()
    if row is None:
        return None
    ids = [{"scheme": s, "value": v} for s, v in con.execute(
        "SELECT scheme, value FROM source_identifiers WHERE source_id = ? ORDER BY scheme, value", (row[0],))]
    conflicts = [{"scheme": s, "value": v, "owner": o} for s, v, o in con.execute(
        "SELECT c.scheme, c.value, o.citekey FROM source_identifier_conflicts c JOIN sources o ON o.id = c.owner_source_id "
        "WHERE c.source_id = ? ORDER BY c.scheme, c.value", (row[0],))]
    return {"citekey": row[1], "name": row[2], "identifiers": ids, "conflicts": conflicts}


@app.command("ids", help="Show a source's identifiers (DOI, ISBN, ISSN, PMID, arXiv, other) and any it shares with another source.")
def cmd_ids(citekey: str, as_json: bool = JSON_OPT):
    import knowledge
    try:
        con = knowledge.connect()
    except knowledge.DatabaseNotFound as e:
        _fail(e)
    try:
        info = source_ids(con, citekey)
    except Exception as e:
        _fail(f"{e} (rebuild knowledge.db with the current schema)")
    finally:
        con.close()
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
