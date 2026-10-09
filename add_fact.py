#!/usr/bin/env python3
"""Append one ad hoc fact to general_facts.json (in knowledge-private, not
this repo), enforcing the shape 11_seed_general_facts.py expects (valid
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
import argparse, os, sys, uuid

import clock
import add_fact_store
import new_fact
import privacy
import privacy_store
from add_fact_store import append_record, append_records  # noqa: F401  (re-exported: facts_batch, tests)
from fact_rules import FRESHNESS_VALUES, SOURCE_KEY_RE, VALID_TRUST, VALID_VISIBILITY, VIA_RE  # noqa: F401  (re-exported: callers use add_fact.VALID_TRUST etc.)
from new_fact import (AddResult, DATE_RE, DataFileError, NewFact, SUBJECT_RE, VALID_NEW_STATUS)  # noqa: F401  (re-exported)
from paths import KNOWLEDGE_DB_DIR, PRIVATE_DATA_DIR
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(PRIVATE_DATA_DIR, "general_facts.json")
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")


def new_source_key():
    return "f-" + uuid.uuid4().hex[:12]


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
    p.add_argument("--visibility", default="private", help="private (default) or normal. Only 'normal' facts may leave the local machine; when unsure, leave it private.")
    p.add_argument("--trust-rationale")
    p.add_argument("--notes")
    p.add_argument("--recheck-by", help="A date (YYYY-MM-DD) or short phrase like 'next physical'. Required unless --no-decay.")
    p.add_argument("--recheck-rationale", help="Why this recheck date; with --no-decay, why the fact does not decay (required).")
    p.add_argument("--source-citekey", help="Must already exist in the `sources` table.")
    p.add_argument("--source-locator")
    p.add_argument("--source-quote", help="The words that justified the fact (with --captured-via, no --source-citekey is needed).")
    p.add_argument("--captured-via", help="Where the fact came from: cli, mcp, migrate-memory, ... With 'mcp', --session-id and --source-quote are required.")
    p.add_argument("--session-id", help="The conversation/session the fact was captured in.")
    p.add_argument("--allow-dirty", action="store_true", help="Skip the clean-tree check on knowledge-private (deliberate batch edits only); the commit still contains only the facts file.")
    p.set_defaults(is_personal=True)
    return p.parse_args(argv)


def validate_fact(fact, db_path=None):
    """Returns (errors, notes). Never prints. Same rules and messages the CLI has always had."""
    db_path = DB_PATH if db_path is None else db_path
    errors, notes = new_fact.check_fact(fact), []
    if fact.source_citekey:
        error, note = add_fact_store.citekey_problem(db_path, fact.source_citekey)
        if error:
            errors.append(error)
        if note:
            notes.append(note)
    return errors, notes


def build_entry(fact, visibility=None):
    """The JSON entry 11_seed_general_facts.py expects (see new_fact.build_entry). `visibility` is the
    resolved value from privacy.resolve_visibility (append_fact passes it); left None it falls back
    to the caller's request."""
    return new_fact.build_entry(fact, visibility, new_source_key(), clock.now_iso())


def resolve_privacy(fact, data_path, db_path):
    """The privacy rules (privacy_rules.json next to the data file) applied to one fact. Subject
    context comes from the db when it has a subjects table (parents for tag inheritance, and the set
    of known subjects) plus subjects already in the facts file; with no usable db the unknown-subject
    rule is not enforced (nothing to compare against). Raises privacy.PrivacyRulesError."""
    rules = privacy_store.load_rules(os.path.join(os.path.dirname(os.path.abspath(data_path)), privacy.RULES_FILENAME))
    parents, known = add_fact_store.subject_tree(db_path)
    if known is not None:
        known |= add_fact_store.file_subjects(data_path)
    return privacy.resolve_visibility(fact.subject, fact.statement, fact.visibility,
                                      rules.with_context(parents=parents, known_subjects=known),
                                      extra_text=(fact.notes, fact.trust_rationale, fact.recheck_rationale,
                                                  fact.source_quote, fact.source_locator))


def append_fact(fact, data_path=None, db_path=None):
    """Validate and append one fact. Returns an AddResult; never prints or exits. The stored
    visibility is the most restrictive of the request, the subject tag and the keyword list (#31)."""
    data_path = DATA_PATH if data_path is None else data_path
    db_path = DB_PATH if db_path is None else db_path
    errors, notes = validate_fact(fact, db_path)
    if errors:
        return AddResult(ok=False, errors=errors, notes=notes)
    try:
        resolution = resolve_privacy(fact, data_path, db_path)
    except (privacy.PrivacyRulesError, DataFileError) as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    entry = build_entry(fact, visibility=resolution.visibility)
    try:
        total = append_record(data_path, entry)
    except DataFileError as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    except OSError as e:
        return AddResult(ok=False, errors=[f"could not write {data_path}: {e}"], notes=notes)
    return AddResult(ok=True, notes=notes, entry=entry, total=total, privacy=resolution)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    fact = NewFact(
        statement=args.statement, subject=args.subject, trust_level=args.trust_level, no_decay=args.no_decay, domain=args.domain,
        is_original_claim=args.is_original_claim, is_personal=args.is_personal, visibility=args.visibility,
        trust_rationale=args.trust_rationale, notes=args.notes, recheck_by=args.recheck_by,
        recheck_rationale=args.recheck_rationale, source_citekey=args.source_citekey,
        source_locator=args.source_locator, source_quote=args.source_quote,
        captured_via=args.captured_via, session_id=args.session_id,
    )
    # Git safety net (#10): only when the data file lives in a git repo (a non-git data dir,
    # e.g. a throwaway test layout, is written without committing, with a note). append_fact
    # itself stays free of git side effects.
    try:
        repo = find_repo(os.path.dirname(DATA_PATH))
        if repo is None:
            print(f"note: {os.path.dirname(DATA_PATH)} is not inside a git repository; the change will not be committed.", file=sys.stderr)
        elif not args.allow_dirty:
            ensure_clean_tree(repo)
    except PrivateGitError as e:
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
            commit = commit_private_change([DATA_PATH], message, repo)
            detached = is_detached(repo)
        except PrivateGitError as e:
            print(f"error: the fact IS in {DATA_PATH} but is NOT committed: {e}", file=sys.stderr)
            return 3
        print(f"Committed {commit} in {repo}: {message}")
        if detached:
            print(f"warning: {repo} has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
