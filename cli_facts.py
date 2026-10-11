"""`knowledge.py search | show | history | subjects | facts`: the read commands over the built db. Thin printers over
`ctx.obj.queries` (the container's read side); registered in knowledge.py with `app.add_typer(cli_facts.app)`."""
import enum
import revisions
import validtime
from fact_reads_port import FactQueryError
from fact_rules import KIND_VALUES
from typing import Optional

import typer

from cli_common import (INCLUDE_PENDING_OPT, JSON_OPT, _emit_json, _fail, _personal, _print_fact_lines, _read)

app = typer.Typer(pretty_exceptions_enable=False, rich_markup_mode=None)


class Trust(str, enum.Enum):
    verified = "verified"
    high = "high"
    medium = "medium"
    low = "low"
    unverified = "unverified"
    disputed = "disputed"


Kind = enum.Enum("Kind", {k: k for k in KIND_VALUES}, type=str)  # #39; one source of truth: KIND_VALUES


ENTITY_OPT = typer.Option(None, "--entity", help="Only facts that mention this entity (its id, name or an alias; #42).")


IDENTIFIER_OPT = typer.Option(None, "--identifier", help="Only facts that cite a source with this DOI, ISBN, ISSN, PMID, arXiv or PMC id (#48; a `doi:` style prefix is optional).")


VALID_AT_OPT = typer.Option(None, "--valid-at", help="Only facts that were true on this date, YYYY-MM-DD (valid time, #40; not --as-of, which is when the db learned it).")


KIND_OPT = typer.Option(None, "--kind", help="Only facts of this kind: " + ", ".join(KIND_VALUES) + ".")


class Status(str, enum.Enum):
    pending = "pending"
    active = "active"
    superseded = "superseded"
    retracted = "retracted"


@app.command("search", help="Full-text search over facts (statement/trust_rationale/notes).")
def cmd_search(ctx: typer.Context, 
    terms: str,
    subject: Optional[str] = None,
    trust: Optional[Trust] = None,
    limit: int = 20,
    personal_only: bool = typer.Option(False, "--personal-only"),
    not_personal: bool = typer.Option(False, "--not-personal"),
    status: Optional[Status] = typer.Option(None, "--status", help="Only facts with this status (overrides the active-only default and --include-pending)."),
    include_pending: bool = INCLUDE_PENDING_OPT,
    kind: Optional[Kind] = KIND_OPT,
    valid_at: Optional[str] = VALID_AT_OPT,
    entity: Optional[str] = ENTITY_OPT,
    identifier: Optional[str] = IDENTIFIER_OPT,
    as_json: bool = JSON_OPT,
):
    c = ctx.obj
    personal = _personal(personal_only, not_personal)
    try:
        rows = _read(c.queries.search_facts, terms, subject=subject, trust=trust.value if trust else None,
                      personal=personal, limit=limit, status=status.value if status else None,
                      include_pending=include_pending, kind=kind.value if kind else None, valid_at=valid_at, entity=entity, identifier=identifier)
    except ValueError as e:
        _fail(e)
    except FactQueryError as e:
        _fail(f"search failed: {e}")
    _emit_json(rows) if as_json else _print_fact_lines(rows)


def _print_revision_state(r):
    print(f"\n{r['statement']}\n")
    for label, key in (("Trust rationale", "trust_rationale"), ("Notes", "notes"), ("Superseded by", "superseded_by"),
                       ("Freshness", "freshness"), ("Kind", "kind"), ("Applies to", "applies_to")):
        if r[key]:
            print(f"{label}: {r[key]}")
    if r["recheck_by"]:
        print(f"Recheck by: {r['recheck_by']}" + (f"  ({r['recheck_rationale']})" if r["recheck_rationale"] else ""))


def _print_validity(r, valid_at=None, verdict=None):
    """Valid-time line (#40); printed when the fact has an interval or a --valid-at question was asked."""
    span = validtime.describe(r["valid_from"], r["valid_to"])
    if span:
        print(f"Valid: {span}")
    if valid_at is not None:
        print(f"Valid on {valid_at}: {'yes' if verdict else 'NO'}")


def _show_as_of(c, fact_id, as_of, as_json, valid_at=None):
    try:
        kind, r = _read(c.queries.fact_as_of, fact_id, as_of)
    except ValueError as e:
        _fail(e)
    if kind == "no-fact":
        _fail(f"no fact with id {fact_id}")
    if kind == "no-history":
        _fail(f"fact {fact_id} has no revision history (rebuild, or it predates revisions)")
    if kind == "not-yet":
        _fail(f"fact {fact_id} did not exist yet at {as_of}")
    verdict = None if valid_at is None else validtime.contains(r["valid_from"], r["valid_to"], valid_at)
    if as_json:
        _emit_json(r if verdict is None else {**r, "valid_at": {"date": valid_at, "valid": verdict}})
        if verdict is False:
            raise typer.Exit(1)
        return
    via = f" via {r['changed_via']}" if r["changed_via"] else ""
    print(f"Fact #{r['fact_id']}  [{r['subject']}]  as of {as_of}  (revision {r['revision']}, changed {r['changed_at']}{via})")
    print(f"trust={r['trust_level']}  visibility={r['visibility']}  status={r['status']}")
    if r["change_reason"]:
        print(f"Reason: {r['change_reason']}")
    _print_revision_state(r)
    _print_validity(r, valid_at, verdict)
    if verdict is False:
        raise typer.Exit(1)


@app.command("show", help="Show one fact in full, with its sources. --as-of DATE shows it as it stood then (end of that day, UTC; or an ISO timestamp).")
def cmd_show(ctx: typer.Context, fact_id: int, as_json: bool = JSON_OPT,
             as_of: Optional[str] = typer.Option(None, "--as-of", help="YYYY-MM-DD (end of that day, UTC) or an ISO-8601 timestamp."),
             valid_at: Optional[str] = typer.Option(None, "--valid-at", help="Also say whether the fact was true on this date, YYYY-MM-DD (valid time, #40). Exit 1 when it was not.")):
    c = ctx.obj
    if valid_at is not None and not validtime.is_valid_date(valid_at):
        _fail(f"--valid-at {valid_at!r} must be a real date, YYYY-MM-DD")
    if as_of is not None:
        return _show_as_of(c, fact_id, as_of, as_json, valid_at)
    f = _read(c.queries.get_fact, fact_id)
    if not f:
        _fail(f"no fact with id {fact_id}")
    verdict = None if valid_at is None else validtime.contains(f["valid_from"], f["valid_to"], valid_at)
    if as_json:
        _emit_json(f if verdict is None else {**f, "valid_at": {"date": valid_at, "valid": verdict}})
        if verdict is False:
            raise typer.Exit(1)
        return

    print(f"Fact #{f['id']}  [{f['subject']}]  trust={f['trust_level']}  personal={bool(f['is_personal'])}  visibility={f['visibility']}  status={f['status']}  kind={f['kind']}")
    print(f"\n{f['statement']}\n")
    if f["trust_rationale"]:
        print(f"Trust rationale: {f['trust_rationale']}")
    if f["notes"]:
        print(f"Notes: {f['notes']}")
    if f["origin_path"]:
        print(f"Origin: {f['origin_path']}")
    if f["recheck_by"]:
        print(f"Recheck by: {f['recheck_by']}" + (f"  ({f['recheck_rationale']})" if f["recheck_rationale"] else ""))
    _print_validity(f, valid_at, verdict)
    if f["applies_to"]:
        print(f"Applies to: {f['applies_to']}")
    if f["source_key"]:
        print(f"Source key: {f['source_key']}")
    if f["captured_via"] or f["session_id"] or f["captured_at"]:
        parts = [f"{label}={f[col]}" for label, col in (("via", "captured_via"), ("session", "session_id"), ("at", "captured_at")) if f[col]]
        print("Captured: " + "  ".join(parts))
    if f["source_quote"]:
        print(f"Source quote: {f['source_quote']}")
    if f["sources"]:
        print("\nSources:")
        for s in f["sources"]:
            loc = f" ({s['locator']})" if s["locator"] else ""
            print(f"  - {s['name']}{loc}")
    if verdict is False:
        raise typer.Exit(1)


def _revision_diff(prev, cur):
    return [f"{k}: {prev[k]!r} -> {cur[k]!r}" for k in revisions.MUTABLE_FIELDS if prev[k] != cur[k]]


@app.command("history", help="Every revision of a fact, oldest first. REF is a fact id (digits) or a source_key.")
def cmd_history(ctx: typer.Context, ref: str, as_json: bool = JSON_OPT):
    c = ctx.obj
    ref = ref.strip()
    if not ref:
        _fail("give a fact id or a source_key")
    key = int(ref) if ref.isascii() and ref.isdigit() else ref
    rows = _read(c.queries.history, key)
    if not rows:
        _fail(f"no revision history for {ref!r} (unknown fact id or source_key, or the fact has no revisions)")
    if as_json:
        _emit_json(rows)
        return
    first = rows[0]
    n = len(rows)
    print(f"Fact #{first['fact_id']}  [{first['subject']}]  source_key={first['source_key']}  ({n} revision{'' if n == 1 else 's'})")
    prev = None
    for r in rows:
        extra = (f"  via={r['changed_via']}" if r["changed_via"] else "") + (f"  session={r['session_id']}" if r["session_id"] else "")
        print(f"\nrev {r['revision']}  {r['changed_at']}{extra}")
        if r["change_reason"]:
            print(f"  reason: {r['change_reason']}")
        if prev is None:
            for k in revisions.MUTABLE_FIELDS:
                if r[k] not in (None, ""):
                    print(f"  {k}: {r[k]}")
        else:
            diff = _revision_diff(prev, r)
            for d in diff or ["(no field changed)"]:
                print(f"  {d}")
        prev = r


@app.command("subjects", help="List subjects (indented under parent) with counts of their active facts.")
def cmd_subjects(ctx: typer.Context, include_pending: bool = INCLUDE_PENDING_OPT, as_json: bool = JSON_OPT):
    c = ctx.obj
    rows = _read(c.queries.list_subjects, include_pending=include_pending)
    if as_json:
        _emit_json(rows)
        return
    for r in rows:
        indent = "  " if r["parent"] else ""
        print(f"{indent}{r['name']:<35} ({r['domain']}, {r['n_facts']} facts)")


@app.command("facts", help="List/filter facts without full-text search. Active facts by default; --include-pending adds pending ones, --status X shows only status X.")
def cmd_facts(ctx: typer.Context, 
    subject: Optional[str] = None,
    trust: Optional[Trust] = None,
    status: Optional[Status] = None,
    limit: int = 50,
    personal_only: bool = typer.Option(False, "--personal-only"),
    not_personal: bool = typer.Option(False, "--not-personal"),
    include_pending: bool = INCLUDE_PENDING_OPT,
    kind: Optional[Kind] = KIND_OPT,
    valid_at: Optional[str] = VALID_AT_OPT,
    entity: Optional[str] = ENTITY_OPT,
    identifier: Optional[str] = IDENTIFIER_OPT,
    as_json: bool = JSON_OPT,
):
    c = ctx.obj
    personal = _personal(personal_only, not_personal)
    try:
        rows = _read(c.queries.list_facts, subject=subject, trust=trust.value if trust else None,
                      status=status.value if status else None, include_pending=include_pending,
                      personal=personal, limit=limit, kind=kind.value if kind else None, valid_at=valid_at, entity=entity, identifier=identifier)
    except ValueError as e:
        _fail(e)
    _emit_json(rows) if as_json else _print_fact_lines(rows)
