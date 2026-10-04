"""Typer commands for the fact lifecycle (#8): supersede, retract.

Thin printers over lifecycle.py (which never prints or exits). Registered in knowledge.py by one
line, `app.add_typer(cli_lifecycle.app)`; this module must not import knowledge at import time
(knowledge imports it), so the db is opened through a lazy import.

Exit codes (same as review): 0 changed or already in the requested state, 1 refused / unknown ref /
any error, 3 the revision was written but the commit failed. `--json` prints LifecycleResult.to_json().
A change takes effect in knowledge.db after the next rebuild (`knowledge.py build`).
"""
import json
import sys

import typer

import lifecycle

app = typer.Typer(pretty_exceptions_enable=False, rich_markup_mode=None)

JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")
ALLOW_DIRTY_OPT = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (the commit still holds only the revision log).")
REASON_OPT = typer.Option(..., "--reason", help="Why (required; recorded in the revision history).")


def _open_db():
    """Read-only connection to the built db, or None when there is none (source_keys still resolve)."""
    import knowledge
    try:
        return knowledge.connect()
    except knowledge.DatabaseNotFound:
        return None


def _report(res, as_json):
    if as_json:
        print(json.dumps(res.to_json(), indent=2, ensure_ascii=False))
    else:
        for n in res.notes:
            print(f"note: {n}", file=sys.stderr)
        if res.errors:
            for e in res.errors:
                print(f"error: {e}", file=sys.stderr)
        elif res.outcome == "unchanged":
            print(f"no change: {res.source_key}: {res.reason}")
        elif res.changed:
            r = res.revision
            where = f", committed {res.commit}" if res.commit else ""
            print(f"{res.verb}: {res.source_key} -> revision {r['revision']}{where}. Rebuild to update knowledge.db.")
            if res.detached:
                print("note: HEAD is detached in knowledge-private; that commit is easy to lose.", file=sys.stderr)
        else:
            print(f"error: {res.outcome}: {res.reason}", file=sys.stderr)
        if res.commit_error:
            print(f"error: {res.commit_error}", file=sys.stderr)
    if res.commit_error:
        raise typer.Exit(3)
    if not res.ok:
        raise typer.Exit(1)


def _call(fn, *args, as_json, **kw):
    db = _open_db()
    try:
        res = fn(*args, db=db, **kw)
    finally:
        if db is not None:
            db.close()
    _report(res, as_json)


@app.command("supersede", help="Mark a fact superseded by another (appends a revision; never edits the entry).")
def cmd_supersede(ref: str, by: str = typer.Option(..., "--by", help="The replacement fact (id or source_key)."),
                  reason: str = REASON_OPT, allow_dirty: bool = ALLOW_DIRTY_OPT, as_json: bool = JSON_OPT):
    _call(lifecycle.supersede, ref, by, reason, allow_dirty=allow_dirty, as_json=as_json)


@app.command("retract", help="Mark a fact retracted (appends a revision; never edits the entry).")
def cmd_retract(ref: str, reason: str = REASON_OPT, allow_dirty: bool = ALLOW_DIRTY_OPT, as_json: bool = JSON_OPT):
    _call(lifecycle.retract, ref, reason, allow_dirty=allow_dirty, as_json=as_json)
