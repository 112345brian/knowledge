"""`knowledge.py audit-claims | build-info | audit-source-status | audit-sources`: the premise and provenance audits.
Thin printers over `ctx.obj.queries`; they exit 1 when an audit finds something."""
import sys
from fact_reads_port import FactQueryError
from typing import Optional

import typer

from cli_common import (JSON_OPT, _emit_json, _fail, _read)

app = typer.Typer(pretty_exceptions_enable=False, rich_markup_mode=None)


@app.command("audit-claims", help="List claims whose premises (cited facts) are superseded, retracted or past recheck_by. Exit 1 when a grounds/backing premise is stale; a stale rebuttal is listed as information only (it strengthens the claim).")
def cmd_audit_claims(ctx: typer.Context, as_json: bool = JSON_OPT):
    c = ctx.obj
    try:
        rows, unparsed = _read(c.queries.audit_claims)
    except FactQueryError as e:
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
def cmd_build_info(ctx: typer.Context, compare: Optional[str] = typer.Option(None, "--compare", help="Another knowledge.db to compare input hashes against."),
                   as_json: bool = JSON_OPT):
    c = ctx.obj
    try:
        mine, other, diff = _read(c.queries.build_info, compare)
    except FactQueryError as e:
        _fail(f"{e} (rebuild knowledge.db with the current schema)")
    if mine is None:
        _fail("this database recorded no build info (rebuild it)")
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
def cmd_audit_source_status(ctx: typer.Context, as_json: bool = JSON_OPT):
    c = ctx.obj
    try:
        rows, orphans = _read(c.queries.audit_source_status)
    except FactQueryError as e:
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
def cmd_audit_sources(ctx: typer.Context, as_json: bool = JSON_OPT):
    c = ctx.obj
    try:
        rows, unbaselined = _read(c.queries.audit_sources)
    except FactQueryError as e:
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
              f"(run `python3 -m ingest.backfill_extracted_hashes`).", file=sys.stderr)
    if rows:
        raise typer.Exit(1)
