#!/usr/bin/env python3
"""Append one ad hoc fact to general_facts.json (in knowledge-private, not
this repo), enforcing the shape seed_general_facts.py expects (valid
trust_level, kebab-case subject name, a real source citekey if one is
given). This is the sanctioned way to add a "raw", not-project-specific
fact -- one with no vault or bodybuilding project behind it -- without
hand-editing the JSON or the live db.

Doesn't touch knowledge.db itself: rerun `python3 build.py` afterward to
pick the new fact up, same as any other data/*.json change.

Usage:
    python3 add_fact.py "Statement text." --subject some-subject --trust medium \
        --recheck-by 2027-01-01
    python3 add_fact.py "..." --subject x --trust medium --no-decay \\
        --recheck-rationale "a birthdate does not change"
    python3 add_fact.py "..." --subject x --trust high --domain general \
        --notes "..." --recheck-by 2026-12-01 --recheck-rationale "..." \
        --source-citekey some-existing-citekey --source-locator "p. 4"

Freshness is mandatory (#7). A new fact needs EITHER --recheck-by (a date or short phrase) OR
--no-decay together with --recheck-rationale saying why it does not decay; neither, or both, is
refused. --no-decay is NOT an excuse to default --trust to 'verified': the fact still needs an
honestly considered trust level (cross-checked against an ID vs. typed from memory). The schema
only enforces that one of the two is present; it cannot judge whether a fact really does not
decay. Staleness only: a fact that was wrong when typed is --trust's job.
"""
import argparse, os, sys

import add_fact_service
import add_fact_store
import clock
import ids
import new_fact  # noqa: F401  (add_fact.new_fact)
import privacy  # noqa: F401  (kept importable as add_fact.privacy)
import privacy_store
from add_fact_store import append_record, append_records  # noqa: F401  (re-exported: facts_batch, tests)
from fact_rules import FRESHNESS_VALUES, KIND_VALUES, SOURCE_KEY_RE, VALID_TRUST, VALID_VISIBILITY, VIA_RE  # noqa: F401  (re-exported: callers use add_fact.VALID_TRUST etc.)
from new_fact import (AddResult, DATE_RE, DataFileError, NewFact, SUBJECT_RE, VALID_NEW_STATUS)  # noqa: F401  (re-exported)
from paths import KNOWLEDGE_DB_DIR, PRIVATE_DATA_DIR
from ports import Ports, bind
import private_git

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(PRIVATE_DATA_DIR, "general_facts.json")
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")


class _Defaults:
    """Where the facts file and the db are, read from this module on every call (tests repoint them)."""
    data_path = property(lambda self: DATA_PATH)
    db_path = property(lambda self: DB_PATH)


PORTS = Ports(git=private_git, facts_file=add_fact_store, rules=privacy_store, clock=clock, ids=ids, defaults=_Defaults())


def new_source_key():
    return ids.new_source_key()


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("statement", help="The fact itself, as a full sentence.")
    p.add_argument("--subject", required=True, help="kebab-case subject name, e.g. 'car-maintenance'. Reused if it already exists, otherwise created.")
    p.add_argument("--trust", required=True, choices=sorted(VALID_TRUST), dest="trust_level")
    p.add_argument("--domain", default="general", help="Only applies if --subject doesn't exist yet (default: general).")
    p.add_argument("--original-claim", action="store_true", dest="is_original_claim", help="This is your own conclusion, not something a source states.")
    p.add_argument("--not-personal", action="store_false", dest="is_personal", help="Mark as not about the user personally (default: personal).")
    p.add_argument("--no-decay", action="store_true", dest="no_decay",
                   help="Assert this fact does not decay (a birthdate, a completed purchase); requires --recheck-rationale "
                        "and excludes --recheck-by. It does NOT justify --trust verified.")
    p.add_argument("--kind", default="unclassified", choices=KIND_VALUES,
                   help="What kind of assertion this is (default: unclassified). Self-reported guidance.")
    p.add_argument("--valid-from", dest="valid_from", help="When the fact became true: YYYY, YYYY-MM or YYYY-MM-DD (#40). Omit if unknown.")
    p.add_argument("--valid-to", dest="valid_to", help="When it stopped being true (same formats; same value as --valid-from for a point in time). Omit if still true as far as known.")
    p.add_argument("--applies-to", dest="applies_to", help="Who or what the fact applies to (a population or condition the source states, e.g. 'adult men'); never guess one (#45).")
    p.add_argument("--visibility", default="private", help="private (default) or normal. Only 'normal' facts may leave the local machine; when unsure, leave it private.")
    p.add_argument("--trust-rationale")
    p.add_argument("--notes")
    p.add_argument("--recheck-by", help="A date (YYYY-MM-DD) or short phrase like 'next physical'. Required unless --no-decay.")
    p.add_argument("--recheck-rationale", help="Why this recheck date; with --no-decay, why the fact does not decay (required).")
    p.add_argument("--source-citekey", help="Must already exist in the `sources` table.")
    p.add_argument("--source-locator")
    p.add_argument("--origin-path", help="The file this fact was extracted from; its SHA-256 is recorded as the fixity baseline (#38) when the file is readable.")
    p.add_argument("--source-quote", help="The words that justified the fact (with --captured-via, no --source-citekey is needed).")
    p.add_argument("--captured-via", help="Where the fact came from: cli, mcp, migrate-memory, ... With 'mcp', --session-id and --source-quote are required.")
    p.add_argument("--session-id", help="The conversation/session the fact was captured in.")
    p.add_argument("--allow-dirty", action="store_true", help="Skip the clean-tree check on knowledge-private (deliberate batch edits only); the commit still contains only the facts file.")
    p.set_defaults(is_personal=True)
    return p.parse_args(argv)


validate_fact = bind(add_fact_service.validate_fact, PORTS)
build_entry = bind(add_fact_service.build_entry, PORTS)
resolve_privacy = bind(add_fact_service.resolve_privacy, PORTS)
canonicalize_subject = bind(add_fact_service.canonicalize_subject, PORTS)
append_fact = bind(add_fact_service.append_fact, PORTS)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    fact = NewFact(
        statement=args.statement, subject=args.subject, trust_level=args.trust_level, no_decay=args.no_decay, domain=args.domain,
        is_original_claim=args.is_original_claim, is_personal=args.is_personal, visibility=args.visibility, kind=args.kind, valid_from=args.valid_from, valid_to=args.valid_to, applies_to=args.applies_to,
        trust_rationale=args.trust_rationale, notes=args.notes, recheck_by=args.recheck_by,
        recheck_rationale=args.recheck_rationale, source_citekey=args.source_citekey,
        source_locator=args.source_locator, source_quote=args.source_quote, origin_path=args.origin_path,
        captured_via=args.captured_via, session_id=args.session_id,
    )
    # Git safety net (#10): only when the data file lives in a git repo (a non-git data dir,
    # e.g. a throwaway test layout, is written without committing, with a note). append_fact
    # itself stays free of git side effects.
    try:
        repo = PORTS.git.find_repo(os.path.dirname(DATA_PATH))
        if repo is None:
            print(f"note: {os.path.dirname(DATA_PATH)} is not inside a git repository; the change will not be committed.", file=sys.stderr)
        elif not args.allow_dirty:
            PORTS.git.ensure_clean_tree(repo)
    except PORTS.git.PrivateGitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    result = append_fact(fact)
    for note in result.notes:
        print(note, file=sys.stderr)
    if not result.ok:
        for e in result.errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    if result.privacy is not None and result.privacy.raised_above_request:
        print(f"note: stored as private although {fact.visibility} was requested -- {result.privacy.explain()}", file=sys.stderr)
    print(f"Added to {os.path.relpath(DATA_PATH, HERE)} ({result.total} facts total). Run `python3 build.py` to rebuild knowledge.db.")
    if repo is not None:
        message = f"add-fact: {fact.subject} ({fact.trust_level})"
        try:
            commit = PORTS.git.commit_private_change([DATA_PATH], message, repo)
            detached = PORTS.git.is_detached(repo)
        except PORTS.git.PrivateGitError as e:
            print(f"error: the fact IS in {DATA_PATH} but is NOT committed: {e}", file=sys.stderr)
            return 3
        print(f"Committed {commit} in {repo}: {message}")
        if detached:
            print(f"warning: {repo} has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
