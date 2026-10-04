#!/usr/bin/env python3
"""Single entry point for this repo.

    python3 knowledge.py build [--check]
    python3 knowledge.py add-fact "statement" --subject x --trust medium ...
    python3 knowledge.py clean-concerts
    python3 knowledge.py search "some terms" [--subject x] [--trust high] [--personal-only|--not-personal] [--limit N]
    python3 knowledge.py show <fact_id>
    python3 knowledge.py subjects
    python3 knowledge.py facts [--subject x] [--trust high] [--status active] [--personal-only|--not-personal] [--limit N]

`build`, `add-fact`, and `clean-concerts` are thin dispatches to the existing
standalone scripts (build.py, add_fact.py, clean_concerts_csv.py) -- those
still run fine on their own; this just gives one name to remember. `search`,
`show`, `subjects`, and `facts` are new: nothing queried knowledge.db before
this except ad hoc sqlite3/DB Browser.
"""
import argparse, os, pathlib, sqlite3, subprocess, sys

from paths import KNOWLEDGE_DB_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")
VALID_TRUST = {"verified", "high", "medium", "low", "unverified", "disputed"}


def cmd_build(args):
    cmd = [sys.executable, os.path.join(HERE, "build.py")]
    if args.check:
        cmd.append("--check")
    return subprocess.call(cmd)


def cmd_add_fact(args):
    return subprocess.call([sys.executable, os.path.join(HERE, "add_fact.py")] + args.add_fact_args)


def cmd_clean_concerts(args):
    return subprocess.call([sys.executable, os.path.join(HERE, "clean_concerts_csv.py")])


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


def _personal(args):
    return True if args.personal_only else (False if args.not_personal else None)


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


# ---- thin CLI printers ----

def _print_fact_lines(rows):
    if not rows:
        print("No matches.")
        return
    for r in rows:
        flag = "" if r["status"] == "active" else f" [{r['status']}]"
        print(f"#{r['id']:<5} [{r['subject']}] ({r['trust_level']}){flag}  {r['statement']}")


def cmd_search(args):
    con = connect()
    try:
        rows = search_facts(con, args.terms, subject=args.subject, trust=args.trust,
                            personal=_personal(args), limit=args.limit)
    finally:
        con.close()
    _print_fact_lines(rows)


def cmd_show(args):
    con = connect()
    try:
        f = get_fact(con, args.fact_id)
    finally:
        con.close()
    if not f:
        print(f"error: no fact with id {args.fact_id}", file=sys.stderr)
        return 1

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


def cmd_subjects(args):
    con = connect()
    try:
        rows = list_subjects(con)
    finally:
        con.close()
    for r in rows:
        indent = "  " if r["parent"] else ""
        print(f"{indent}{r['name']:<35} ({r['domain']}, {r['n_facts']} facts)")


def cmd_facts(args):
    con = connect()
    try:
        rows = list_facts(con, subject=args.subject, trust=args.trust, status=args.status,
                          personal=_personal(args), limit=args.limit)
    finally:
        con.close()
    _print_fact_lines(rows)


def _add_personal_flags(sp):
    grp = sp.add_mutually_exclusive_group()
    grp.add_argument("--personal-only", action="store_true")
    grp.add_argument("--not-personal", action="store_true")


def main(argv=None):
    p = argparse.ArgumentParser(prog="knowledge.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Rebuild knowledge.db from schema.sql + scripts + data/.")
    b.add_argument("--check", action="store_true", help="Build into a throwaway file and report counts; live DB untouched.")
    b.set_defaults(func=cmd_build)

    af = sub.add_parser("add-fact", help="Append an ad hoc fact to data/general_facts.json.", add_help=False)
    af.set_defaults(func=cmd_add_fact)

    cc = sub.add_parser("clean-concerts", help="Clean concerts.csv in place (dedupes rows).")
    cc.set_defaults(func=cmd_clean_concerts)

    s = sub.add_parser("search", help="Full-text search over facts (statement/trust_rationale/notes).")
    s.add_argument("terms")
    s.add_argument("--subject")
    s.add_argument("--trust", choices=sorted(VALID_TRUST))
    s.add_argument("--limit", type=int, default=20)
    _add_personal_flags(s)
    s.set_defaults(func=cmd_search)

    sh = sub.add_parser("show", help="Show one fact in full, with its sources.")
    sh.add_argument("fact_id", type=int)
    sh.set_defaults(func=cmd_show)

    su = sub.add_parser("subjects", help="List subjects (indented under parent) with fact counts.")
    su.set_defaults(func=cmd_subjects)

    fa = sub.add_parser("facts", help="List/filter facts without full-text search.")
    fa.add_argument("--subject")
    fa.add_argument("--trust", choices=sorted(VALID_TRUST))
    fa.add_argument("--status", choices=["active", "superseded", "retracted"])
    fa.add_argument("--limit", type=int, default=50)
    _add_personal_flags(fa)
    fa.set_defaults(func=cmd_facts)

    args, extra = p.parse_known_args(argv)
    if args.command == "add-fact":
        args.add_fact_args = extra
    elif extra:
        p.error(f"unrecognized arguments: {' '.join(extra)}")

    try:
        return args.func(args) or 0
    except DatabaseNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
