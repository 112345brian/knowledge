"""`knowledge.py review-pending | approve | reject`: the review queue. Thin printers over `ctx.obj.queries` and
`ctx.obj.review`; exit codes as documented on each command."""
import sys
from fact_reads_port import FactQueryError
from typing import List
from typing import Optional

import typer

from cli_common import (ALLOW_DIRTY_OPT_REVIEW, JSON_OPT, _emit_json, _fail, _read)

app = typer.Typer(pretty_exceptions_enable=False, rich_markup_mode=None)


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
def cmd_review_pending(ctx: typer.Context, as_json: bool = JSON_OPT):
    c = ctx.obj
    try:
        rows = _read(c.queries.pending)
    except FactQueryError as e:
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
def cmd_approve(ctx: typer.Context, 
    refs: Optional[List[str]] = typer.Argument(None, help="Fact ids or source_keys."),
    reason: str = typer.Option("approved", "--reason", help="Why; recorded as the revision's change_reason."),
    all_: bool = typer.Option(False, "--all", help="Approve every pending fact."),
    allow_dirty: bool = ALLOW_DIRTY_OPT_REVIEW,
    as_json: bool = JSON_OPT,
):
    c = ctx.obj
    refs = list(refs or [])
    if all_ and refs:
        raise typer.BadParameter("give REF... or --all, not both")
    if not all_ and not refs:
        raise typer.BadParameter("give at least one REF, or --all")
    res = c.review.approve(["all"] if all_ else refs, reason=reason, via="cli", allow_dirty=allow_dirty, db=c.queries.existing_db_path())
    _report_review(res, as_json)


@app.command("reject", help="Reject pending facts (REF is a fact id or a source_key): appends a 'retracted' revision. "
                            "Only pending facts change; others are skipped.")
def cmd_reject(ctx: typer.Context, 
    refs: List[str] = typer.Argument(..., help="Fact ids or source_keys."),
    reason: str = typer.Option(..., "--reason", help="Why; recorded as the revision's change_reason."),
    allow_dirty: bool = ALLOW_DIRTY_OPT_REVIEW,
    as_json: bool = JSON_OPT,
):
    c = ctx.obj
    res = c.review.reject(list(refs), reason, via="cli", allow_dirty=allow_dirty, db=c.queries.existing_db_path())
    _report_review(res, as_json)
