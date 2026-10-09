"""Typer commands for the subject hierarchy (#43): `subject list|show|alias|describe|deprecate`.

Thin printers over subjects.py (which never prints or exits). Registered in knowledge.py with
`app.add_typer(cli_subjects.app, name="subject")`; this module must not import knowledge at import time
(knowledge imports it), so the db is opened through a lazy import.

`list` and `show` read the BUILT db (like `subjects`); `alias`, `describe` and `deprecate` edit
subjects.json in knowledge-private through the same git flow as `privacy tag` (clean tree required unless
--allow-dirty, one commit of that file only, --dry-run writes nothing) and take effect in knowledge.db
after the next rebuild. Exit codes: 0 done or no change, 1 refused / error, 3 written but not committed.
"""
import json
import sys

import typer

import subjects

app = typer.Typer(help="The subject hierarchy: relation types, descriptions, aliases, deprecation. "
                       "Edits go to subjects.json in knowledge-private (rebuild to apply).",
                  pretty_exceptions_enable=False, rich_markup_mode=None)

JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")
ALLOW_DIRTY_OPT = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (the commit still holds only subjects.json).")
DRY_RUN_OPT = typer.Option(False, "--dry-run", help="Report what would change and commit; write nothing.")


def _fail(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def _connect():
    import knowledge
    try:
        return knowledge.connect()
    except knowledge.DatabaseNotFound as e:
        _fail(e)


def _known():
    """{subject name: domain} from the built db, or {} when there is none."""
    import knowledge
    try:
        con = knowledge.connect()
    except knowledge.DatabaseNotFound:
        return {}
    try:
        return {n: d for n, d in con.execute("SELECT name, domain FROM subjects")}
    except Exception:
        return {}
    finally:
        con.close()


def _rows(con):
    rows = [dict(r) for r in con.execute(
        """SELECT s.id, s.name, s.domain, p.name AS parent, s.parent_relation AS relation, s.description,
                  s.deprecated, r.name AS replaced_by,
                  (SELECT COUNT(*) FROM facts f WHERE f.subject_id = s.id) AS n_facts
           FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id LEFT JOIN subjects r ON r.id = s.replaced_by_subject_id
           ORDER BY s.name""")]
    aliases = {}
    for sid, alias in con.execute("SELECT subject_id, alias FROM subject_aliases ORDER BY alias"):
        aliases.setdefault(sid, []).append(alias)
    for r in rows:
        r["aliases"] = aliases.get(r["id"], [])
        r["deprecated"] = bool(r["deprecated"])
        if not r["parent"]:
            r["relation"] = None
    return rows


@app.command("list", help="List subjects with parent, relation type, deprecation, aliases and fact counts (from the built db).")
def cmd_list(as_json: bool = JSON_OPT):
    con = _connect()
    try:
        rows = _rows(con)
    finally:
        con.close()
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for r in rows:
        bits = [f"{r['n_facts']} fact{'' if r['n_facts'] == 1 else 's'}"]
        if r["parent"]:
            bits.append(f"{r['relation']} of {r['parent']}")
        if r["aliases"]:
            bits.append("aka " + ", ".join(r["aliases"]))
        if r["deprecated"]:
            bits.append("DEPRECATED" + (f" -> {r['replaced_by']}" if r["replaced_by"] else ""))
        print(f"{r['name']}  [{r['domain']}]  " + "; ".join(bits))


@app.command("show", help="One subject in full: description, relation, aliases, children, deprecation. NAME may be an alias.")
def cmd_show(name: str, as_json: bool = JSON_OPT):
    con = _connect()
    try:
        rows = _rows(con)
    finally:
        con.close()
    by_name = {r["name"]: r for r in rows}
    via_alias = False
    if name not in by_name:
        owner = next((r for r in rows if name in r["aliases"]), None)
        if owner is None:
            _fail(f"unknown subject {name!r}")
        name, via_alias = owner["name"], True
    r = dict(by_name[name], children=[c["name"] for c in rows if c["parent"] == name])
    if as_json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return
    print(f"{r['name']}  [{r['domain']}]" + ("  (resolved from an alias)" if via_alias else ""))
    if r["description"]:
        print(f"Description: {r['description']}")
    if r["parent"]:
        print(f"Parent: {r['parent']} ({r['relation']})")
    if r["children"]:
        print("Children: " + ", ".join(r["children"]))
    if r["aliases"]:
        print("Aliases: " + ", ".join(r["aliases"]))
    if r["deprecated"]:
        print("DEPRECATED" + (f"; use {r['replaced_by']} instead" if r["replaced_by"] else "; no replacement named"))
    print(f"Facts: {r['n_facts']}")


def _report(res, as_json):
    if as_json:
        print(json.dumps(res.to_json(), indent=2, ensure_ascii=False))
    else:
        for n in res.notes:
            print(f"note: {n}", file=sys.stderr)
        for e in res.errors:
            print(f"error: {e}", file=sys.stderr)
        if res.ok and res.dry_run:
            print(f"dry run: would {res.what} in {res.path}" + (f" and commit {res.message!r}" if res.message else "") + "; nothing written")
        elif res.ok and res.changed:
            print(f"Updated {res.path}: {res.what}. Rebuild to update knowledge.db.")
            if res.commit:
                print(f"Committed {res.commit}: {res.message}")
            if res.detached:
                print("warning: HEAD is detached in knowledge-private; that commit is not on any branch.", file=sys.stderr)
        if res.commit_error:
            print(f"error: {res.commit_error}", file=sys.stderr)
    if res.commit_error:
        raise typer.Exit(3)
    if not res.ok:
        raise typer.Exit(1)


@app.command("alias", help="Give a subject another name. New facts filed under the alias are stored under the subject.")
def cmd_alias(name: str, alias: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    known = _known()
    res = subjects.edit_file(lambda e: subjects.add_alias(e, name, alias, known=set(known), domain=known.get(name)),
                             f"add alias {alias} to {name}", f"subjects: alias {alias} -> {name}", allow_dirty, dry_run)
    _report(res, as_json)


@app.command("describe", help="Set a subject's scope note (blank text clears it).")
def cmd_describe(name: str, text: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    known = _known()
    res = subjects.edit_file(lambda e: subjects.describe(e, name, text, known=set(known), domain=known.get(name)),
                             f"set description of {name}", f"subjects: describe {name}", allow_dirty, dry_run)
    _report(res, as_json)


@app.command("deprecate", help="Refuse a subject for NEW facts (existing facts keep it); --replaced-by names the one to use instead.")
def cmd_deprecate(name: str, replaced_by: str = typer.Option(None, "--replaced-by", help="The subject to use instead."),
                  allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    known = _known()
    res = subjects.edit_file(lambda e: subjects.deprecate(e, name, replaced_by, known=set(known), domain=known.get(name)),
                             f"deprecate {name}" + (f" (use {replaced_by})" if replaced_by else ""),
                             f"subjects: deprecate {name}", allow_dirty, dry_run)
    _report(res, as_json)
