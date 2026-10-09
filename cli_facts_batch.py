"""`knowledge.py add-facts`: the Typer command over facts_batch.add_facts (#32, CLI-first rule #34).

Registered from knowledge.py by one line (`cli_facts_batch.register(app)`); kept here so that file
stays small and this module never imports it (no cycle).

    knowledge.py add-facts facts.json            # a JSON list of {"statement", "subject", ...}
    cat facts.json | knowledge.py add-facts -    # same list on stdin (what the MCP tool receives)
    knowledge.py add-facts facts.json --dry-run  # each item's visibility and the rule behind it; writes nothing

Exit codes (as add_fact.py): 0 ok; 1 any error (bad JSON, invalid/refused item, dirty tree...);
3 the facts were written but the git commit failed.
"""
import enum
import json
import sys
from typing import Optional

import typer

import facts_batch


class NewStatus(str, enum.Enum):
    active = "active"
    pending = "pending"


def _fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(1)


def _read_items(source):
    try:
        if source in (None, "-"):
            text = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as f:
                text = f.read()
    except OSError as e:
        _fail(f"could not read {source}: {e}")
    except UnicodeDecodeError as e:
        _fail(f"{source or 'stdin'} is not valid UTF-8 ({e})")
    try:
        items = json.loads(text)
    except json.JSONDecodeError as e:
        _fail(f"input is not valid JSON ({e})")
    if not isinstance(items, list):
        _fail("input must be a JSON list of fact objects")
    return items


def _clip(text, n=70):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _print_result(result):
    for note in result.notes:
        print(note, file=sys.stderr)
    for e in result.errors:
        print(f"error: {e}", file=sys.stderr)
    for i in result.items:
        head = f"{i.index + 1}. {i.outcome}"
        if i.visibility:
            head += f" [{i.visibility}]"
        print(f"{head} {i.subject or '?'}: {_clip(i.statement)}")
        if i.rule:
            print(f"     visibility: {i.rule}" + (" (raised above the request)" if i.raised else ""))
        for e in i.errors:
            print(f"     {e}")
    if result.dry_run:
        print(f"dry run: {len([i for i in result.items if i.outcome == 'would_save'])} of {len(result.items)} would be saved; nothing written.")
    elif result.saved:
        print(f"Saved {len(result.saved)} of {len(result.items)} ({result.total} facts total). Run `python3 build.py` to rebuild knowledge.db.")
    if result.commit:
        print(f"Committed {result.commit}")
    if result.detached:
        print("warning: the private repo has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
    if result.commit_error:
        print(f"error: {result.commit_error}", file=sys.stderr)


def register(app):
    @app.command("add-facts", help="Add a batch of reviewed facts from a JSON list (FILE, or - / no argument for stdin) in one atomic write and one commit. --dry-run shows each item's resulting visibility and the rule behind it.")
    def cmd_add_facts(
        file: Optional[str] = typer.Argument(None, help="JSON file; '-' or omitted reads stdin."),
        status: NewStatus = typer.Option(NewStatus.active, "--status", help="active (default: a human reviewed them) or pending (queue for review)."),
        captured_via: str = typer.Option(facts_batch.DEFAULT_CAPTURED_VIA, "--captured-via", help="Provenance token, e.g. cli, mcp. With 'mcp' every item needs source_quote and --session-id is required."),
        session_id: Optional[str] = typer.Option(None, "--session-id", help="The conversation the facts were captured in."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Print each item's resulting visibility and the rule behind it; write and commit nothing."),
        allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (the commit still holds only the facts file)."),
        as_json: bool = typer.Option(False, "--json", help="Machine-readable result."),
    ):
        items = _read_items(file)
        result = facts_batch.add_facts(items, status=status.value, captured_via=captured_via, session_id=session_id,
                                       dry_run=dry_run, allow_dirty=allow_dirty)
        if as_json:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        else:
            _print_result(result)
        if result.commit_error:
            raise typer.Exit(3)
        if not result.ok:
            raise typer.Exit(1)
