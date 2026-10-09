"""Typer commands for entities (#42): `entity list|show|add|alias|tag|untag|migrate-keywords`.

Thin printers over entities.py / entity_tools.py (which never print or exit). Registered in knowledge.py with
`app.add_typer(cli_entities.app, name="entity")`; this module must not import knowledge at import time
(knowledge imports it), so the db is opened through a lazy import.

`list` and `show` read entities.json (the source of truth, so they work before a rebuild) and add fact link
counts from the built db when there is one. The edit commands go through the same git flow as `privacy tag`
(clean tree required unless --allow-dirty, one commit of the files touched, --dry-run writes nothing) and
take effect in knowledge.db after the next rebuild. Exit codes: 0 done or no change, 1 refused / error,
3 written but not committed.
"""
import enum
import json
import sys
from typing import List, Optional

import typer

import entities as entity_usecases

app = typer.Typer(help="Entities: the people, organizations, places, projects and substances facts are about. "
                       "A private entity makes every fact that mentions it private. Edits go to entities.json "
                       "in knowledge-private (rebuild to apply).",
                  pretty_exceptions_enable=False, rich_markup_mode=None)

JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")
ALLOW_DIRTY_OPT = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (the commit still holds only the files touched).")
DRY_RUN_OPT = typer.Option(False, "--dry-run", help="Report what would change and commit; write nothing.")

EType = enum.Enum("EType", {t: t for t in entity_usecases.ENTITY_TYPES}, type=str)


def _fail(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def _load():
    try:
        return entity_usecases.read_file()
    except entity_usecases.EntitiesError as e:
        _fail(e)


def _link_counts():
    """{entity_key: n facts} from the built db; {} when there is none (or it predates entities)."""
    return entity_usecases.link_counts()


def _row(e, counts):
    return {**e, "n_facts": counts.get(e["id"])}


@app.command("list", help="List entities (from entities.json) with type, privacy and linked-fact counts (from the built db).")
def cmd_list(as_json: bool = JSON_OPT):
    counts = _link_counts()
    rows = [_row(e, counts) for e in sorted(_load(), key=lambda e: e["id"])]
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("No entities (entities.json is absent or empty).")
    for r in rows:
        bits = [r["type"], "PRIVATE" if r["private"] else "normal"]
        if r["aliases"]:
            bits.append("aka " + ", ".join(r["aliases"]))
        if r["n_facts"] is not None:
            bits.append(f"{r['n_facts']} fact{'' if r['n_facts'] == 1 else 's'}")
        print(f"{r['id']}  {r['canonical_name']}  [" + "; ".join(bits) + "]")


@app.command("show", help="One entity in full. REF is its id, canonical name or an alias.")
def cmd_show(ref: str, as_json: bool = JSON_OPT):
    es = _load()
    e = entity_usecases.find(es, ref)
    if e is None:
        _fail(f"unknown entity {ref!r}")
    row = _row(e, _link_counts())
    if as_json:
        print(json.dumps(row, indent=2, ensure_ascii=False))
        return
    print(f"{e['canonical_name']}  ({e['id']})")
    print(f"Type: {e['type']}    Privacy: {'PRIVATE (facts that mention it are private)' if e['private'] else 'normal'}")
    if e["aliases"]:
        print("Aliases: " + ", ".join(e["aliases"]))
    if e["external_id"]:
        print(f"External id: {e['external_id']}")
    if e["notes"]:
        print(f"Notes: {e['notes']}")
    if row["n_facts"] is not None:
        print(f"Linked facts: {row['n_facts']}")


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


@app.command("add", help="Add an entity. Its id is derived from the name. --private makes facts that mention it private.")
def cmd_add(name: str, type: EType = typer.Option(EType.other, "--type", help="person | organization | place | project | substance | other"),
            alias: Optional[List[str]] = typer.Option(None, "--alias", help="Another name for it (repeatable)."),
            private: bool = typer.Option(False, "--private", help="Facts that mention it become private."),
            external_id: Optional[str] = typer.Option(None, "--external-id", help="Optional outside identifier, e.g. wikidata:Q42."),
            notes: Optional[str] = typer.Option(None, "--notes"),
            allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    res = entity_usecases.add(name, type.value, alias or (), private, external_id, notes, allow_dirty, dry_run)
    _report(res, as_json)


@app.command("alias", help="Add another name for an entity. Its facts are found by every name.")
def cmd_alias(ref: str, alias: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    res = entity_usecases.alias(ref, alias, allow_dirty, dry_run)
    _report(res, as_json)


@app.command("tag", help="Make an entity private: every fact that mentions it becomes private (after a rebuild).")
def cmd_tag(ref: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    res = entity_usecases.tag(ref, allow_dirty, dry_run)
    _report(res, as_json)


@app.command("untag", help="Make an entity non-private again. Facts already stored private stay private.")
def cmd_untag(ref: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    res = entity_usecases.untag(ref, allow_dirty, dry_run)
    _report(res, as_json)


@app.command("migrate-keywords", help="Convert every privacy keyword into a private entity (type other) and clear the keyword list, in one commit. Checked first: nothing becomes less private.")
def cmd_migrate(keep_keywords: bool = typer.Option(False, "--keep-keywords", help="Only add the entities; leave the keyword list as it is."),
                allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT, as_json: bool = JSON_OPT):
    _report(entity_usecases.migrate_keywords(allow_dirty=allow_dirty, dry_run=dry_run, keep_keywords=keep_keywords), as_json)
