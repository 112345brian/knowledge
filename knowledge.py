#!/usr/bin/env python3
"""Single entry point for this repo (Typer CLI; run `uv sync` once, then `uv run python knowledge.py --help`).

    knowledge.py build [--check]
    knowledge.py add-fact "statement" --subject x --trust medium ...
    knowledge.py clean-concerts
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

`build`, `add-fact`, and `clean-concerts` are thin dispatches to the existing
standalone scripts (build.py, add_fact.py, ingest/clean_concerts_csv.py) -- those
still run fine on their own, without typer; this just gives one name to
remember. `search`, `show`, `subjects`, and `facts` query knowledge.db.
"""
import enum
import json
import sys
from typing import List, Optional

import typer

import claims_store
import fact_queries
import validtime
import privacy
import review
import revisions
import rules_edit
import script_runner
from fact_rules import KIND_VALUES
from fact_queries import (DB_PATH, DatabaseNotFound, QueryError, connect, existing_db_path,  # noqa: F401  (library functions kept importable here)
                          fact_as_of, get_fact, list_facts, list_subjects, search_facts)

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


# ---- thin CLI printers (Typer) ----

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False, rich_markup_mode=None)
import cli_facts_batch; cli_facts_batch.register(app)  # `add-facts` (#32)


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
        applies = f"  (applies to: {r['applies_to']})" if r.get("applies_to") else ""
        print(f"#{r['id']:<5} [{r['subject']}] ({r['trust_level']}){flag}  {r['statement']}{applies}")


JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")
ALLOW_DIRTY_OPT_REVIEW = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on the data repo (deliberate batch edits only); the commit still contains only fact_revisions.jsonl.")
INCLUDE_PENDING_OPT = typer.Option(False, "--include-pending", help="Also show pending (unreviewed) facts; by default only active facts are listed.")


@app.command("build", help="Rebuild knowledge.db from schema.sql + scripts + data/.")
def cmd_build(check: bool = typer.Option(False, "--check", help="Build into a throwaway file and report counts; live DB untouched.")):
    raise typer.Exit(script_runner.run_script("build.py", ["--check"] if check else []))


@app.command("add-fact", help="Append an ad hoc fact to data/general_facts.json. Every argument is forwarded to add_fact.py (see add_fact.py --help).",
             context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []})
def cmd_add_fact(ctx: typer.Context):
    raise typer.Exit(script_runner.run_script("add_fact.py", list(ctx.args)))


@app.command("clean-concerts", help="Clean concerts.csv in place (dedupes rows).")
def cmd_clean_concerts():
    raise typer.Exit(script_runner.run_module("ingest.clean_concerts_csv"))


@app.command("search", help="Full-text search over facts (statement/trust_rationale/notes).")
def cmd_search(
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
    personal = _personal(personal_only, not_personal)
    try:
        rows = _query(search_facts, terms, subject=subject, trust=trust.value if trust else None,
                      personal=personal, limit=limit, status=status.value if status else None,
                      include_pending=include_pending, kind=kind.value if kind else None, valid_at=valid_at, entity=entity, identifier=identifier)
    except ValueError as e:
        _fail(e)
    except QueryError as e:
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


def _show_as_of(fact_id, as_of, as_json, valid_at=None):
    try:
        kind, r = _query(fact_as_of, fact_id, as_of)
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
def cmd_show(fact_id: int, as_json: bool = JSON_OPT,
             as_of: Optional[str] = typer.Option(None, "--as-of", help="YYYY-MM-DD (end of that day, UTC) or an ISO-8601 timestamp."),
             valid_at: Optional[str] = typer.Option(None, "--valid-at", help="Also say whether the fact was true on this date, YYYY-MM-DD (valid time, #40). Exit 1 when it was not.")):
    if valid_at is not None and not validtime.is_valid_date(valid_at):
        _fail(f"--valid-at {valid_at!r} must be a real date, YYYY-MM-DD")
    if as_of is not None:
        return _show_as_of(fact_id, as_of, as_json, valid_at)
    f = _query(get_fact, fact_id)
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
def cmd_history(ref: str, as_json: bool = JSON_OPT):
    ref = ref.strip()
    if not ref:
        _fail("give a fact id or a source_key")
    key = int(ref) if ref.isascii() and ref.isdigit() else ref
    rows = _query(fact_queries.get_history, key)
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


@app.command("audit-claims", help="List claims whose premises (cited facts) are superseded, retracted or past recheck_by. Exit 1 when a grounds/backing premise is stale; a stale rebuttal is listed as information only (it strengthens the claim).")
def cmd_audit_claims(as_json: bool = JSON_OPT):
    def run(con):
        return claims_store.audit_claims(con), claims_store.unparseable_rechecks(con)
    try:
        rows, unparsed = _query(run)
    except QueryError as e:
        _fail(f"audit failed: {e} (rebuild knowledge.db with the current schema)")
    if as_json:
        _emit_json({"stale_premises": rows, "unparseable_rechecks": unparsed})
    elif not rows:
        print("No stale premises.")
    else:
        for r in rows:
            kind = f" ({r['inference_type']})" if r["inference_type"] else ""
            print(f"claim #{r['claim_id']}{kind}: {r['claim_statement']}")
            role = "" if r["role"] == "grounds" else f" [{r['role']}]"
            line = f"  fact #{r['fact_id']}{role} {r['reason']}"
            if r["reason"].endswith("past_recheck_by"):
                line += f" ({r['recheck_by']})"
            elif r["superseded_by_fact_id"]:
                line += f" (by fact #{r['superseded_by_fact_id']})"
            if r["severity"] == "info":
                line += " -- informational: a stale rebuttal only strengthens the claim"
            print(f"{line}: {r['fact_statement']}")
            if r["link_note"]:
                print(f"    linked because: {r['link_note']}")
    if unparsed and not as_json:
        ids = ", ".join(f"#{u['fact_id']} ({u['recheck_by']!r})" for u in unparsed)
        print(f"note: {len(unparsed)} cited fact(s) have a recheck_by that is not an ISO date, so the audit cannot judge them: {ids}", file=sys.stderr)
    if any(r["severity"] == "weakens" for r in rows):
        raise typer.Exit(1)


@app.command("build-info", help="Which inputs and code produced this knowledge.db: the latest build row and its input manifest. --compare OTHER.db lists inputs that differ (exit 1 when any).")
def cmd_build_info(compare: Optional[str] = typer.Option(None, "--compare", help="Another knowledge.db to compare input hashes against."),
                   as_json: bool = JSON_OPT):
    try:
        mine = _query(fact_queries.latest_build_info)
        other = None
        if compare is not None:
            try:
                con = connect(compare)
            except DatabaseNotFound as e:
                _fail(e)
            try:
                other = fact_queries.latest_build_info(con)
            finally:
                con.close()
    except QueryError as e:
        _fail(f"{e} (rebuild knowledge.db with the current schema)")
    if mine is None:
        _fail("this database recorded no build info (rebuild it)")
    diff = fact_queries.compare_build_info(other, mine) if compare is not None else None
    if as_json:
        _emit_json({**mine, "compared_with": compare, "changed_inputs": diff})
    elif diff is not None:
        print("No input differs." if not diff else "\n".join(f"{d['key']}: {d['change']}" for d in diff))
    else:
        b = mine["build"]
        print(f"Built {b['built_at']}  schema v{b['schema_version']}  python {b['python_version']}  sqlite {b['sqlite_version']}")
        for label, c, d in (("code", b["code_commit"], b["code_dirty"]), ("knowledge-private", b["private_commit"], b["private_dirty"])):
            print(f"{label}: {c or 'not in git'}" + (" (uncommitted changes)" if d else ""))
        for i in mine["inputs"]:
            print(f"  {i['input_key']}: " + (f"{i['sha256'][:12]}  {i['size_bytes']} bytes" if i["state"] == "present" else "missing"))
    if diff:
        raise typer.Exit(1)


@app.command("audit-source-status", help="List active/pending facts that cite a retracted source, one under an expression of concern, or one that was superseded (with its replacement). Exit 1 when any are found.")
def cmd_audit_source_status(as_json: bool = JSON_OPT):
    try:
        rows, orphans = _query(fact_queries.audit_source_status)
    except QueryError as e:
        _fail(f"audit failed: {e} (rebuild knowledge.db with the current schema)")
    if as_json:
        _emit_json({"flagged_sources": rows, "superseded_without_replacement": orphans})
    elif not rows:
        print("No fact cites a retracted, doubtful or superseded source.")
    else:
        for r in rows:
            when = f" since {r['status_date']}" if r["status_date"] else ""
            line = f"fact #{r['fact_id']} cites {r['citekey'] or r['source_name']}: {r['reason']}{when}"
            if r["reason"] == "superseded":
                line += " by " + (", ".join(x["citekey"] or x["name"] for x in r["replacement"]))
                if r["derived"]:
                    line += " (from a `replaces` relation; the source itself says " + r["status"] + ")"
            print(line)
            print(f"  {r['fact_statement']}")
            if r["status_note"]:
                print(f"  note: {r['status_note']}")
    if orphans and not as_json:
        names = ", ".join(f"{o['citekey'] or o['name']} (facts {', '.join(map(str, o['fact_ids']))})" for o in orphans)
        print(f"note: {len(orphans)} source(s) are marked superseded but nothing replaces them, so there is nothing to move to: {names}", file=sys.stderr)
    if rows:
        raise typer.Exit(1)


@app.command("audit-sources", help="List facts whose source file changed, went missing or moved since the fact was extracted (SHA-256 baseline). Exit 1 when any are found.")
def cmd_audit_sources(as_json: bool = JSON_OPT):
    try:
        rows, unbaselined = _query(fact_queries.audit_sources)
    except QueryError as e:
        _fail(f"audit failed: {e} (rebuild knowledge.db with the current schema)")
    if as_json:
        _emit_json({"changed_sources": rows, "no_baseline": unbaselined})
    elif not rows:
        print("No source file has changed since extraction.")
    else:
        for r in rows:
            line = f"fact #{r['fact_id']} {r['reason']}: {r['path']}"
            if r["moved_to"]:
                line += f" -> {r['moved_to']}"
            print(line)
            print(f"  {r['statement']}")
    if unbaselined and not as_json:
        print(f"note: {len(unbaselined)} fact(s) cite a source file but have no recorded baseline hash, so the audit cannot judge them "
              f"(run backfill_extracted_hashes.py).", file=sys.stderr)
    if rows:
        raise typer.Exit(1)


@app.command("subjects", help="List subjects (indented under parent) with counts of their active facts.")
def cmd_subjects(include_pending: bool = INCLUDE_PENDING_OPT, as_json: bool = JSON_OPT):
    rows = _query(list_subjects, include_pending=include_pending)
    if as_json:
        _emit_json(rows)
        return
    for r in rows:
        indent = "  " if r["parent"] else ""
        print(f"{indent}{r['name']:<35} ({r['domain']}, {r['n_facts']} facts)")


@app.command("facts", help="List/filter facts without full-text search. Active facts by default; --include-pending adds pending ones, --status X shows only status X.")
def cmd_facts(
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
    personal = _personal(personal_only, not_personal)
    try:
        rows = _query(list_facts, subject=subject, trust=trust.value if trust else None,
                      status=status.value if status else None, include_pending=include_pending,
                      personal=personal, limit=limit, kind=kind.value if kind else None, valid_at=valid_at, entity=entity, identifier=identifier)
    except ValueError as e:
        _fail(e)
    _emit_json(rows) if as_json else _print_fact_lines(rows)


# ---- review of pending facts (#6): `review-pending`, `approve`, `reject` ----

def _review_json(res):
    return {"ok": res.ok, "items": [dict(ref=i.ref, outcome=i.outcome, source_key=i.source_key, reason=i.reason,
                                         revision=i.revision) for i in res.items],
            "errors": res.errors, "notes": res.notes, "commit": res.commit,
            "commit_error": res.commit_error, "detached": res.detached}


def _review_exit_code(res):
    """3 when revisions were written but not committed (the state that needs attention);
    else 1 for a batch error or any unknown/error item; else 0. Skipped items are fine."""
    if res.commit_error is not None:
        return 3
    return 0 if res.ok else 1


def _report_review(res, as_json):
    if as_json:
        _emit_json(_review_json(res))
    else:
        for e in res.errors:
            print(f"error: {e}", file=sys.stderr)
        for i in res.items:
            if i.outcome in ("approved", "rejected"):
                print(f"{i.outcome} {i.ref}" + (f" ({i.source_key})" if i.source_key and i.source_key != i.ref else ""))
            else:
                print(f"{i.outcome} {i.ref}: {i.reason}", file=sys.stderr if i.outcome in ("unknown", "error") else sys.stdout)
        if not res.items and not res.errors:
            print("Nothing to do.")
        for n in res.notes:
            print(f"note: {n}", file=sys.stderr)
        if res.commit:
            print(f"Committed {res.commit}")
        if res.commit_error:
            print(f"error: {res.commit_error}", file=sys.stderr)
        if res.detached:
            print("warning: the data repo has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
        if res.changed:
            print("note: knowledge.db is not rebuilt yet; run `knowledge.py build` to see the change.", file=sys.stderr)
    code = _review_exit_code(res)
    if code:
        raise typer.Exit(code)


@app.command("review-pending", help="List pending (unreviewed) facts, oldest first.")
def cmd_review_pending(as_json: bool = JSON_OPT):
    try:
        rows = _query(review.list_pending)
    except QueryError as e:
        _fail(f"review-pending failed: {e} (rebuild knowledge.db with the current schema)")
    if as_json:
        _emit_json(rows)
        return
    if not rows:
        print("No pending facts.")
        return
    for r in rows:
        via = f"  via={r['captured_via']}" if r["captured_via"] else ""
        print(f"#{r['id']:<5} [{r['subject']}] ({r['trust_level']})  {r['source_key']}  added {r['date_added']}{via}")
        print(f"       {r['statement']}")
        if r["source_quote"]:
            print(f"       quote: {r['source_quote']}")
    print(f"\n{len(rows)} pending. Approve with `approve REF...` (or `--all`), reject with `reject REF... --reason TEXT`.")


@app.command("approve", help="Approve pending facts (REF is a fact id or a source_key): appends an 'active' revision "
                             "and commits it to the data repo. Only pending facts change; others are skipped.")
def cmd_approve(
    refs: Optional[List[str]] = typer.Argument(None, help="Fact ids or source_keys."),
    reason: str = typer.Option("approved", "--reason", help="Why; recorded as the revision's change_reason."),
    all_: bool = typer.Option(False, "--all", help="Approve every pending fact."),
    allow_dirty: bool = ALLOW_DIRTY_OPT_REVIEW,
    as_json: bool = JSON_OPT,
):
    refs = list(refs or [])
    if all_ and refs:
        raise typer.BadParameter("give REF... or --all, not both")
    if not all_ and not refs:
        raise typer.BadParameter("give at least one REF, or --all")
    res = review.approve(["all"] if all_ else refs, reason=reason, via="cli", allow_dirty=allow_dirty, db=existing_db_path())
    _report_review(res, as_json)


@app.command("reject", help="Reject pending facts (REF is a fact id or a source_key): appends a 'retracted' revision. "
                            "Only pending facts change; others are skipped.")
def cmd_reject(
    refs: List[str] = typer.Argument(..., help="Fact ids or source_keys."),
    reason: str = typer.Option(..., "--reason", help="Why; recorded as the revision's change_reason."),
    allow_dirty: bool = ALLOW_DIRTY_OPT_REVIEW,
    as_json: bool = JSON_OPT,
):
    res = review.reject(list(refs), reason, via="cli", allow_dirty=allow_dirty, db=existing_db_path())
    _report_review(res, as_json)


# ---- privacy (#31): `privacy check|rules|tag|untag|add-keyword|remove-keyword` ----

privacy_app = typer.Typer(help="Privacy rules: check what visibility a statement would get, and edit the rules "
                               "(subject tags and keywords). The rules file is edited only through these commands.",
                          pretty_exceptions_enable=False, rich_markup_mode=None)
app.add_typer(privacy_app, name="privacy")


class Requested(str, enum.Enum):
    normal = "normal"
    private = "private"


def _load_rules():
    try:
        return rules_edit.load_rules()
    except privacy.PrivacyRulesError as e:
        _fail(e)


def _db_context(rules):
    """`rules` with the subject tree and the known-subject list from the live db (see
    fact_queries.subject_context). No db -> empty context, so the unknown-subject rule is not enforced
    (nothing to compare against)."""
    ctx = fact_queries.subject_context()
    if ctx is None:
        return rules
    parents, known = ctx
    return rules.with_context(parents=parents, known_subjects=known)


def _resolution_json(res):
    return {"visibility": res.visibility, "raised_above_request": res.raised_above_request,
            "explanation": res.explain(),
            "reasons": [{"kind": r.kind, "detail": r.detail, "forces_private": r.forces_private} for r in res.reasons]}


@privacy_app.command("check", help="Resolve the visibility a statement would be stored with, and name the rule that raised it.")
def cmd_privacy_check(statement: str,
                      subject: str = typer.Option(..., "--subject", help="Subject the fact would be filed under."),
                      requested: Requested = typer.Option(Requested.normal, "--requested",
                                                          help="What the caller asks for; default normal shows what the rules alone do."),
                      as_json: bool = JSON_OPT):
    res = privacy.check(subject, statement, _db_context(_load_rules()), requested=requested.value)
    _emit_json(_resolution_json(res)) if as_json else print(res.explain())


@privacy_app.command("rules", help="Show the loaded privacy rules (subject tags and keywords).")
def cmd_privacy_rules(as_json: bool = JSON_OPT):
    path = rules_edit.rules_path()
    rules = _load_rules()
    exists = rules_edit.rules_file_exists(path)
    if as_json:
        _emit_json({"path": path, "exists": exists, "version": privacy.VERSION,
                    "subject_tags": dict(sorted(rules.subject_tags.items())), "keywords": sorted(rules.keywords)})
        return
    print(f"Privacy rules: {path}" + ("" if exists else "  (no rules file; empty rules)"))
    if rules.subject_tags:
        print("subject tags:")
        for name, tag in sorted(rules.subject_tags.items()):
            print(f"  {name}: {tag}")
    else:
        print("subject tags: (none)")
    if rules.keywords:
        print("keywords:")
        for kw in sorted(rules.keywords):
            print(f"  - {kw}")
    else:
        print("keywords: (none)")



def _edit_rules(edit, describe, allow_dirty, dry_run):
    """Shared body of the rules-editing commands: run rules_edit.edit_rules and print what it did."""
    try:
        res = rules_edit.edit_rules(edit, describe, allow_dirty, dry_run)
    except privacy.PrivacyRulesError as e:
        _fail(e)
    except rules_edit.PrivateGitError as e:
        _fail(e)
    if not res.changed:
        print(f"no change: {res.path} already has this rule state")
        return
    if res.not_in_git:
        print(f"note: {res.directory} is not inside a git repository; the change will not be committed.", file=sys.stderr)
    if res.dry_run:
        print(f"dry run: would {res.what} in {res.path}" + (f" and commit {res.message!r}" if res.repo else "") + "; nothing written")
        return
    print(f"Updated {res.path}: {res.what}.")
    if res.repo is None:
        return
    if res.commit_error:
        print(f"error: the rule IS written to {res.path} but is NOT committed: {res.commit_error}", file=sys.stderr)
        raise typer.Exit(3)
    print(f"Committed {res.committed} in {res.repo}: {res.message}")
    if res.detached:
        print(f"warning: {res.repo} has a detached HEAD; that commit is not on any branch.", file=sys.stderr)


ALLOW_DIRTY_OPT = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (deliberate batch edits only); the commit still contains only the rules file.")
DRY_RUN_OPT = typer.Option(False, "--dry-run", help="Report what would change and commit; write nothing.")


class Tag(str, enum.Enum):
    private = "private"
    normal = "normal"


@privacy_app.command("tag", help="Tag a subject private (inherited by its children); --tag normal only registers it as a known subject.")
def cmd_privacy_tag(subject: str, tag: Tag = typer.Option(Tag.private, "--tag"),
                    allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    _edit_rules(lambda r: privacy.tag_subject(r, subject, tag.value),
                lambda old, new: (f"tag subject {subject} {tag.value}", f"privacy: tag {subject} ({tag.value})"),
                allow_dirty, dry_run)


@privacy_app.command("untag", help="Remove a subject's tag.")
def cmd_privacy_untag(subject: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    _edit_rules(lambda r: privacy.untag_subject(r, subject),
                lambda old, new: (f"untag subject {subject}", f"privacy: untag {subject}"),
                allow_dirty, dry_run)


@privacy_app.command("add-keyword", help="Add a name/keyword; statements containing it as a whole word become private. Literal text, case-insensitive.")
def cmd_privacy_add_keyword(keyword: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    def describe(old, new):
        kw = next(k for k in new.keywords if k not in old.keywords)
        return f"add keyword {kw!r}", f"privacy: add keyword {kw!r}"
    _edit_rules(lambda r: privacy.add_keyword(r, keyword), describe, allow_dirty, dry_run)


@privacy_app.command("remove-keyword", help="Remove a keyword. Facts already stored private stay private.")
def cmd_privacy_remove_keyword(keyword: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    def describe(old, new):
        kw = next(k for k in old.keywords if k not in new.keywords)
        return f"remove keyword {kw!r}", f"privacy: remove keyword {kw!r}"
    _edit_rules(lambda r: privacy.remove_keyword(r, keyword), describe, allow_dirty, dry_run)

import cli_sources; app.add_typer(cli_sources.app, name="source")  # source ids (#48)
import cli_entities; app.add_typer(cli_entities.app, name="entity")  # entity list|show|add|alias|tag|untag|migrate-keywords (#42)
import cli_subjects; app.add_typer(cli_subjects.app, name="subject")  # subject list|show|alias|describe|deprecate (#43)
import cli_lifecycle; app.add_typer(cli_lifecycle.app)  # supersede, retract, set-visibility (#8, #23)


import cli_migrate; cli_migrate.register(app)  # migrate-memory (#29)


from cli_inbox import register as _register_inbox; _register_inbox(app, DB_PATH)  # inbox, edit (#33)


def main(argv=None):
    """Run the CLI and return the exit code instead of exiting (used by tests)."""
    try:
        app(args=argv, prog_name="knowledge.py")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
