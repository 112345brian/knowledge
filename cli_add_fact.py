"""`knowledge.py add-fact`: the Typer command over add_fact.record_fact (CLI-first rule #34).

Appends one ad hoc fact to general_facts.json (in knowledge-private, not this repo), enforcing the shape
seed_general_facts.py expects (valid trust_level, kebab-case subject name, a real source citekey if one is
given). This is the sanctioned way to add a "raw", not-project-specific fact without hand-editing the JSON
or the live db. It does not touch knowledge.db: rerun `knowledge.py build` afterward.

    knowledge.py add-fact "Statement text." --subject some-subject --trust medium --recheck-by 2027-01-01
    knowledge.py add-fact "..." --subject x --trust medium --no-decay --recheck-rationale "a birthdate does not change"

Freshness is mandatory (#7): a new fact needs EITHER --recheck-by (a date or short phrase) OR --no-decay
together with --recheck-rationale saying why it does not decay; neither, or both, is refused. --no-decay is
NOT an excuse to default --trust to 'verified'.

Registered from knowledge.py by one line (`cli_add_fact.register(app)`). A thin printer: add_fact.record_fact
validates, writes and commits, and never prints or exits.

Exit codes: 0 added (and committed when the data file is in a git repo); 1 invalid, refused or dirty tree;
3 the fact IS in the file but the git commit failed.
"""
import enum
import sys
from typing import Optional

import typer

import add_fact
from fact_rules import KIND_VALUES, VALID_TRUST
from new_fact import NewFact

Trust = enum.Enum("Trust", [(v, v) for v in sorted(VALID_TRUST)], type=str)
Kind = enum.Enum("Kind", [(v, v) for v in KIND_VALUES], type=str)


HELP = (
    "Append an ad hoc fact to general_facts.json (in knowledge-private), enforcing the shape seed_general_facts.py expects. "
    "Freshness is mandatory (#7). A new fact needs EITHER --recheck-by (a date or short phrase) OR --no-decay together with "
    "--recheck-rationale saying why it does not decay; neither, or both, is refused. --no-decay is NOT an excuse to default "
    "--trust to 'verified': the fact still needs an honestly considered trust level (cross-checked against an ID vs. typed "
    "from memory). The schema only enforces that one of the two is present; it cannot judge whether a fact really does not "
    "decay. Staleness only: a fact that was wrong when typed is --trust's job."
)


def register(app):
    @app.command("add-fact", help=HELP)
    def cmd_add_fact(
        statement: str = typer.Argument(..., help="The fact itself, as a full sentence."),
        subject: str = typer.Option(..., "--subject", help="kebab-case subject name, e.g. 'car-maintenance'. Reused if it already exists, otherwise created."),
        trust_level: Trust = typer.Option(..., "--trust"),
        domain: str = typer.Option("general", "--domain", help="Only applies if --subject doesn't exist yet."),
        is_original_claim: bool = typer.Option(False, "--original-claim", help="This is your own conclusion, not something a source states."),
        not_personal: bool = typer.Option(False, "--not-personal", help="Mark as not about the user personally (default: personal)."),
        no_decay: bool = typer.Option(False, "--no-decay", help="Assert this fact does not decay (a birthdate, a completed purchase); requires --recheck-rationale and excludes --recheck-by. It does NOT justify --trust verified."),
        kind: Kind = typer.Option(Kind.unclassified, "--kind", help="What kind of assertion this is. Self-reported guidance."),
        valid_from: Optional[str] = typer.Option(None, "--valid-from", help="When the fact became true: YYYY, YYYY-MM or YYYY-MM-DD (#40). Omit if unknown."),
        valid_to: Optional[str] = typer.Option(None, "--valid-to", help="When it stopped being true (same formats). Omit if still true as far as known."),
        applies_to: Optional[str] = typer.Option(None, "--applies-to", help="Who or what the fact applies to (a population or condition the source states); never guess one (#45)."),
        visibility: str = typer.Option("private", "--visibility", help="private (default) or normal. Only 'normal' facts may leave the local machine; when unsure, leave it private."),
        trust_rationale: Optional[str] = typer.Option(None, "--trust-rationale"),
        notes: Optional[str] = typer.Option(None, "--notes"),
        recheck_by: Optional[str] = typer.Option(None, "--recheck-by", help="A date (YYYY-MM-DD) or short phrase like 'next physical'. Required unless --no-decay."),
        recheck_rationale: Optional[str] = typer.Option(None, "--recheck-rationale", help="Why this recheck date; with --no-decay, why the fact does not decay (required)."),
        source_citekey: Optional[str] = typer.Option(None, "--source-citekey", help="Must already exist in the `sources` table."),
        source_locator: Optional[str] = typer.Option(None, "--source-locator"),
        origin_path: Optional[str] = typer.Option(None, "--origin-path", help="The file this fact was extracted from; its SHA-256 is recorded as the fixity baseline (#38) when readable."),
        source_quote: Optional[str] = typer.Option(None, "--source-quote", help="The words that justified the fact (with --captured-via, no --source-citekey is needed)."),
        captured_via: Optional[str] = typer.Option(None, "--captured-via", help="Where the fact came from: cli, mcp, migrate-memory, ... With 'mcp', --session-id and --source-quote are required."),
        session_id: Optional[str] = typer.Option(None, "--session-id", help="The conversation/session the fact was captured in."),
        allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (deliberate batch edits only); the commit still contains only the facts file."),
    ):
        fact = NewFact(
            statement=statement, subject=subject, trust_level=trust_level.value, no_decay=no_decay, domain=domain,
            is_original_claim=is_original_claim, is_personal=not not_personal, visibility=visibility, kind=kind.value,
            valid_from=valid_from, valid_to=valid_to, applies_to=applies_to, trust_rationale=trust_rationale, notes=notes,
            recheck_by=recheck_by, recheck_rationale=recheck_rationale, source_citekey=source_citekey,
            source_locator=source_locator, source_quote=source_quote, origin_path=origin_path,
            captured_via=captured_via, session_id=session_id,
        )
        result = add_fact.record_fact(fact, allow_dirty=allow_dirty)
        if result.repo is None and result.refused is None:
            print(f"note: {result.data_dir} is not inside a git repository; the change will not be committed.", file=sys.stderr)
        if result.refused is not None:
            print(f"error: {result.refused}", file=sys.stderr)
            raise typer.Exit(1)
        for note in result.added.notes:
            print(note, file=sys.stderr)
        if not result.ok:
            for e in result.added.errors:
                print(f"error: {e}", file=sys.stderr)
            raise typer.Exit(1)
        added = result.added
        if added.privacy is not None and added.privacy.raised_above_request:
            print(f"note: stored as private although {fact.visibility} was requested -- {added.privacy.explain()}", file=sys.stderr)
        print(f"Added to {add_fact.data_file_label()} ({added.total} facts total). Run `python3 build.py` to rebuild knowledge.db.")
        if result.commit_error is not None:
            print(f"error: the fact IS in {add_fact.data_file_path()} but is NOT committed: {result.commit_error}", file=sys.stderr)
            raise typer.Exit(3)
        if result.repo is not None:
            print(f"Committed {result.commit} in {result.repo}: {result.message}")
            if result.detached:
                print(f"warning: {result.repo} has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
