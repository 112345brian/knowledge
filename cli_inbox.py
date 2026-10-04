"""Typer commands for the inbox (#33): `inbox` (start the local server) and `edit`.

Registered in knowledge.py by one line (`register(app, DB_PATH)`); this module does not import
knowledge.py, so it works the same when knowledge.py runs as `__main__`. Logic lives in the libraries
(library first, #34): `edit` calls lifecycle.edit_fact; `inbox` starts the server in inbox.py.

ARCHITECTURE (#36): this is the ONE CLI module allowed to import the serving layer (`inbox`), because
the `inbox` command has to start the server. It is a launcher: it only calls inbox.serve. The
exception is a single named edge in tach.toml / pyproject.toml; every other CLI module is still
forbidden to import serving.
"""
import json
import sys
import threading
import webbrowser
from typing import Optional

import typer

import lifecycle

# THE one allowed CLI -> serving import (see the module docstring and tach.toml): this command starts the server.
import inbox  # tach-ignore inbox


def _fail(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def register(app, db_path):
    @app.command("inbox", help="Serve the pending-fact review page on 127.0.0.1 ONLY (it shows private facts; never expose it). "
                               "Lists the built db snapshot: rebuild to see new pending facts.")
    def cmd_inbox(port: int = typer.Option(inbox.DEFAULT_PORT, "--port", min=0, max=65535, help="Port on 127.0.0.1 (0 = any free port)."),
                  no_open: bool = typer.Option(False, "--no-open", help="Do not open a browser tab.")):
        def ready(url):
            print(f"Inbox on {url} (127.0.0.1 only; Ctrl-C to stop)", flush=True)
            if not no_open:
                threading.Timer(0.3, webbrowser.open, args=(url,)).start()
        try:
            inbox.serve(db_path, port=port, on_ready=ready)
        except OSError as e:
            _fail(f"could not listen on 127.0.0.1:{port}: {e}")

    @app.command("edit", help="Edit a fact by appending a revision. REF is a fact id (digits) or a source_key. "
                              "Give at least one of the field options and --reason. An empty string clears "
                              "--trust-rationale, --recheck-by or --notes. The subject cannot be changed and "
                              "visibility is never lowered.")
    def cmd_edit(ref: str,
                 statement: Optional[str] = typer.Option(None, "--statement", help="New statement text."),
                 trust: Optional[str] = typer.Option(None, "--trust", help="New trust level."),
                 trust_rationale: Optional[str] = typer.Option(None, "--trust-rationale"),
                 recheck_by: Optional[str] = typer.Option(None, "--recheck-by", help="YYYY-MM-DD"),
                 notes: Optional[str] = typer.Option(None, "--notes"),
                 reason: str = typer.Option(..., "--reason", help="Why (recorded in history)."),
                 allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Commit even if the private repo has other uncommitted changes."),
                 as_json: bool = typer.Option(False, "--json")):
        res = lifecycle.edit_fact(ref, reason, statement=statement, trust_level=trust, trust_rationale=trust_rationale,
                              recheck_by=recheck_by, notes=notes, via="cli", allow_dirty=allow_dirty, db=db_path)
        if as_json:
            print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        else:
            for n in res.notes:
                print(f"note: {n}", file=sys.stderr)
            if res.ok:
                print(f"{res.source_key}: {res.message}" + (f" (committed {res.commit})" if res.commit else ""))
            for e in res.errors:
                print(f"error: {e}", file=sys.stderr)
        if not res.ok:
            raise typer.Exit(3 if res.commit_error else 1)
