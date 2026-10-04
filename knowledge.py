#!/usr/bin/env python3
"""Single entry point for this repo (Typer CLI; run `uv sync` once, then `uv run python knowledge.py --help`).

    knowledge.py build [--check]
    knowledge.py add-fact "statement" --subject x --trust medium ...
    knowledge.py clean-concerts
    knowledge.py search "some terms" [--subject x] [--trust high] [--personal-only|--not-personal] [--limit N] [--json]
    knowledge.py show <fact_id> [--json]
    knowledge.py subjects [--json]
    knowledge.py facts [--subject x] [--trust high] [--status active] [--personal-only|--not-personal] [--limit N] [--json]

Convention (issue #34), for every later command: the library function comes
first (pure, importable, returns data, never prints or exits), the Typer
command second (a thin printer, with `--json` on every read command), the MCP
tool / inbox handler third. `cli_parity.py` registers each action's CLI
command and tests/test_cli_parity.py fails when a registered command is
missing or a future MCP tool / inbox action has no registry entry.

`build`, `add-fact`, and `clean-concerts` are thin dispatches to the existing
standalone scripts (build.py, add_fact.py, clean_concerts_csv.py) -- those
still run fine on their own, without typer; this just gives one name to
remember. `search`, `show`, `subjects`, and `facts` query knowledge.db.
"""
import enum
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
from typing import Optional

import typer

from paths import KNOWLEDGE_DB_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")


class Trust(str, enum.Enum):
    verified = "verified"
    high = "high"
    medium = "medium"
    low = "low"
    unverified = "unverified"
    disputed = "disputed"


class Status(str, enum.Enum):
    active = "active"
    superseded = "superseded"
    retracted = "retracted"


class DatabaseNotFound(FileNotFoundError):
    """knowledge.db doesn't exist yet (it is built, never hand-created)."""


def connect(db_path=None):
    """Open the knowledge db READ-ONLY. Raises DatabaseNotFound if it is missing.

    Everything in this module only reads; writes go through build.py, which
    makes its own connection."""
    path = os.path.abspath(db_path or DB_PATH)
    if not os.path.exists(path):
        raise DatabaseNotFound(f"{path} doesn't exist -- run `python3 knowledge.py build` first")
    con = sqlite3.connect(pathlib.Path(path).as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---- query functions: take a connection, return plain dicts, never print or exit ----

def _filters(sql, params, subject=None, trust=None, status=None, personal=None):
    """Append the shared fact filters. `personal` is True / False / None (no filter)."""
    if subject:
        sql += " AND sub.name = ?"
        params.append(subject)
    if trust:
        sql += " AND f.trust_level = ?"
        params.append(trust)
    if status:
        sql += " AND f.status = ?"
        params.append(status)
    if personal is True:
        sql += " AND f.is_personal = 1"
    elif personal is False:
        sql += " AND f.is_personal = 0"
    return sql


def search_facts(con, terms, subject=None, trust=None, personal=None, limit=20):
    """Full-text search, best match first. Raises sqlite3.OperationalError on FTS syntax errors."""
    sql = """
        SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.statement
        FROM facts_fts
        JOIN facts f ON f.id = facts_fts.rowid
        JOIN subjects sub ON sub.id = f.subject_id
        WHERE facts_fts MATCH ?
    """
    params = [terms]
    sql = _filters(sql, params, subject=subject, trust=trust, personal=personal)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def list_facts(con, subject=None, trust=None, status=None, personal=None, limit=50):
    sql = """SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.statement
             FROM facts f JOIN subjects sub ON sub.id = f.subject_id WHERE 1=1"""
    params = []
    sql = _filters(sql, params, subject=subject, trust=trust, status=status, personal=personal)
    sql += " ORDER BY f.id LIMIT ?"
    params.append(limit)
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_fact(con, fact_id):
    """One fact (all columns, plus subject and origin_path) with a `sources` list of
    {name, locator} dicts; None if there is no such fact."""
    f = con.execute(
        """SELECT f.*, sub.name AS subject, vf.path AS origin_path
           FROM facts f
           JOIN subjects sub ON sub.id = f.subject_id
           LEFT JOIN vault_files vf ON vf.id = f.origin_file_id
           WHERE f.id = ?""",
        (fact_id,),
    ).fetchone()
    if not f:
        return None
    out = dict(f)
    out["sources"] = [dict(s) for s in con.execute(
        """SELECT s.name, fs.locator
           FROM fact_sources fs JOIN sources s ON s.id = fs.source_id
           WHERE fs.fact_id = ?""",
        (f["id"],),
    ).fetchall()]
    return out


def list_subjects(con):
    return [dict(r) for r in con.execute(
        """SELECT s.name, s.domain, p.name AS parent, COUNT(f.id) AS n_facts
           FROM subjects s
           LEFT JOIN subjects p ON p.id = s.parent_id
           LEFT JOIN facts f ON f.subject_id = s.id
           GROUP BY s.id
           ORDER BY s.domain, COALESCE(p.name, s.name), s.name"""
    ).fetchall()]


# ---- thin CLI printers (Typer) ----

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False, rich_markup_mode=None)


def _personal(personal_only, not_personal):
    if personal_only and not_personal:
        raise typer.BadParameter("--personal-only and --not-personal are mutually exclusive")
    return True if personal_only else (False if not_personal else None)


def _fail(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def _query(fn, *a, **kw):
    """Open the db, run one library function, close. A missing db is a one-line error, exit 1."""
    try:
        con = connect()
    except DatabaseNotFound as e:
        _fail(e)
    try:
        return fn(con, *a, **kw)
    finally:
        con.close()


def _emit_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_fact_lines(rows):
    if not rows:
        print("No matches.")
        return
    for r in rows:
        flag = "" if r["status"] == "active" else f" [{r['status']}]"
        print(f"#{r['id']:<5} [{r['subject']}] ({r['trust_level']}){flag}  {r['statement']}")


JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")


@app.command("build", help="Rebuild knowledge.db from schema.sql + scripts + data/.")
def cmd_build(check: bool = typer.Option(False, "--check", help="Build into a throwaway file and report counts; live DB untouched.")):
    cmd = [sys.executable, os.path.join(HERE, "build.py")]
    if check:
        cmd.append("--check")
    raise typer.Exit(subprocess.call(cmd))


@app.command("add-fact", help="Append an ad hoc fact to data/general_facts.json. Every argument is forwarded to add_fact.py (see add_fact.py --help).",
             context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []})
def cmd_add_fact(ctx: typer.Context):
    raise typer.Exit(subprocess.call([sys.executable, os.path.join(HERE, "add_fact.py")] + list(ctx.args)))


@app.command("clean-concerts", help="Clean concerts.csv in place (dedupes rows).")
def cmd_clean_concerts():
    raise typer.Exit(subprocess.call([sys.executable, os.path.join(HERE, "clean_concerts_csv.py")]))


@app.command("search", help="Full-text search over facts (statement/trust_rationale/notes).")
def cmd_search(
    terms: str,
    subject: Optional[str] = None,
    trust: Optional[Trust] = None,
    limit: int = 20,
    personal_only: bool = typer.Option(False, "--personal-only"),
    not_personal: bool = typer.Option(False, "--not-personal"),
    as_json: bool = JSON_OPT,
):
    personal = _personal(personal_only, not_personal)
    try:
        rows = _query(search_facts, terms, subject=subject, trust=trust.value if trust else None,
                      personal=personal, limit=limit)
    except sqlite3.OperationalError as e:
        _fail(f"search failed: {e}")
    _emit_json(rows) if as_json else _print_fact_lines(rows)


@app.command("show", help="Show one fact in full, with its sources.")
def cmd_show(fact_id: int, as_json: bool = JSON_OPT):
    f = _query(get_fact, fact_id)
    if not f:
        _fail(f"no fact with id {fact_id}")
    if as_json:
        _emit_json(f)
        return

    print(f"Fact #{f['id']}  [{f['subject']}]  trust={f['trust_level']}  personal={bool(f['is_personal'])}  status={f['status']}")
    print(f"\n{f['statement']}\n")
    if f["trust_rationale"]:
        print(f"Trust rationale: {f['trust_rationale']}")
    if f["notes"]:
        print(f"Notes: {f['notes']}")
    if f["origin_path"]:
        print(f"Origin: {f['origin_path']}")
    if f["recheck_by"]:
        print(f"Recheck by: {f['recheck_by']}" + (f"  ({f['recheck_rationale']})" if f["recheck_rationale"] else ""))
    if f["sources"]:
        print("\nSources:")
        for s in f["sources"]:
            loc = f" ({s['locator']})" if s["locator"] else ""
            print(f"  - {s['name']}{loc}")


@app.command("subjects", help="List subjects (indented under parent) with fact counts.")
def cmd_subjects(as_json: bool = JSON_OPT):
    rows = _query(list_subjects)
    if as_json:
        _emit_json(rows)
        return
    for r in rows:
        indent = "  " if r["parent"] else ""
        print(f"{indent}{r['name']:<35} ({r['domain']}, {r['n_facts']} facts)")


@app.command("facts", help="List/filter facts without full-text search.")
def cmd_facts(
    subject: Optional[str] = None,
    trust: Optional[Trust] = None,
    status: Optional[Status] = None,
    limit: int = 50,
    personal_only: bool = typer.Option(False, "--personal-only"),
    not_personal: bool = typer.Option(False, "--not-personal"),
    as_json: bool = JSON_OPT,
):
    rows = _query(list_facts, subject=subject, trust=trust.value if trust else None,
                  status=status.value if status else None,
                  personal=_personal(personal_only, not_personal), limit=limit)
    _emit_json(rows) if as_json else _print_fact_lines(rows)


def main(argv=None):
    """Run the CLI and return the exit code instead of exiting (used by tests)."""
    try:
        app(args=argv, prog_name="knowledge.py")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
