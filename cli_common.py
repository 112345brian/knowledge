"""Helpers and option definitions shared by the knowledge.py command modules (cli_facts, cli_audits, cli_review,
cli_privacy, cli_build): printing, the missing-db error, and the options every read command has."""
import json
import sys
from fact_reads_port import DatabaseNotFound

import typer


def _personal(personal_only, not_personal):
    if personal_only and not_personal:
        raise typer.BadParameter("--personal-only and --not-personal are mutually exclusive")
    return True if personal_only else (False if not_personal else None)


def _fail(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def _read(fn, *a, **kw):
    """Run one `queries` function. A missing db is a one-line error, exit 1."""
    try:
        return fn(*a, **kw)
    except DatabaseNotFound as e:
        _fail(e)


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


ALLOW_DIRTY_OPT = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (deliberate batch edits only); the commit still contains only the rules file.")


DRY_RUN_OPT = typer.Option(False, "--dry-run", help="Report what would change and commit; write nothing.")
