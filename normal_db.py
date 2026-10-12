#!/usr/bin/env python3
"""Build knowledge-normal.db (the entry point: it binds `normal_db_service` to the SQLite adapters; the
whitelist and row decisions are in `normal_rules`): a copy of the full DB that CANNOT contain private data (#21).

Used so a remote-served connector never has anything private to leak. The build COPIES a
whitelist INTO a fresh, empty database; it never deletes from a copy of knowledge.db (deletion
leaves old pages, free-list garbage and FTS shadow rows holding the deleted text).

What goes in (everything else is absent, not empty):
  facts            only visibility='normal', on a non-private subject chain, and not caught by the
                   privacy rules passed in (re-checked here, so a rule added after the last ingest
                   still keeps a fact out). Columns: id, subject_id, statement, is_original_claim,
                   trust_level, trust_rationale, status, superseded_by_fact_id (NULL unless that
                   fact is also in), date_added, last_reviewed_at, recheck_by, recheck_rationale,
                   visibility, source_key, freshness (#7; staleness metadata like recheck_by, not
                   private: without it a NULL recheck_by cannot be told from "never reviewed").
                   LEFT OUT on purpose: is_personal (a keyword heuristic that can hint at private
                   topics), notes (free text), origin_file_id (vault path), provided_by,
                   captured_via, session_id, captured_at, source_quote (words from a conversation).
  fact_revisions   revisions of an included fact whose OWN visibility is normal and which pass the
                   rules (an older revision that was private is dropped, leaving a gap in the
                   revision numbers; a fact whose current visibility is private has no history
                   here at all). LEFT OUT: session_id, change_reason, notes. `superseded_by` is
                   kept only when it names an included fact.
  fact_sources     citation rows of included facts only. `locator` and `quote` ARE included: they
                   belong to a normal fact, so they are as visible as the fact. A quote on a
                   private fact's citation of the same source is not copied.
  sources          only sources cited by an included fact (name, citekey, type, url, dates,
                   description, status and edition (#41)). LEFT OUT: origin_path (vault file path), created_at, status_date,
                   status_note (may hold a private URL), original_published_date, source_relations, the custodial-history
                   columns (acquired_at, acquired_via, where_from, acquired_note: a download URL reveals interests, #46)
                   and the fixity columns
                   (content_sha256, size_bytes, file_mtime, mime_type, file_state; #38).
  authors, source_authors, publishers   only those reachable from an included source.
  subjects         subjects used by included facts plus their parent chain (id, name, domain,
                   parent_id). LEFT OUT: the `private` flag column (all are non-private).
  facts_fts, sources_fts, authors_fts   rebuilt over the copied rows.
  v_subjects       view: subjects having at least one included fact, with a count of INCLUDED
                   facts only (never stored, so it cannot go stale or count private facts).
subjects        also description, parent_relation, deprecated, replaced_by_subject_id (only if that subject is
                   included) and subject_aliases (#43); a description or alias that contains a listed keyword
                   is dropped, the subject stays.
entities        only NON-private entities that an included fact mentions (key, name, type, external_id, notes), their
                   aliases and the fact_entities links of included facts (#42). A private entity, its aliases, notes
                   and links are never copied; a name, alias or note containing a listed keyword is dropped.
build_info      the latest row's built_at and schema_version only (#47); commits, dirty flags, versions and build_inputs
                   (input keys) are never copied.
source_identifiers   identifiers of included sources (#48); source_identifier_conflicts is never copied.
Other full-database tables are omitted unless explicitly whitelisted by `normal_rules.TABLES`.
"""
import sys

import leak_scan_store
import normal_db_service
import normal_db_store
import wiring
from normal_db_service import NormalDbDeps
from leak_rules import format_leaks  # noqa: F401  (the public API)
from normal_rules import (FACT_COLS, FTS_TABLES, NORMAL_DB_NAME, NormalDbError, REVISION_COLS, SOURCE_COLS, SOURCE_EDITION,  # noqa: F401  (the public API)
                          TABLES, VIEWS, format_counts)


def deps_for(files):
    """The service's dependencies for these `files` (a BuildFiles) and the SQLite adapters, verified."""
    return wiring.verify(NormalDbDeps(files=files, source=normal_db_store, sink=normal_db_store, leak_scan=leak_scan_store))


def build_normal_db(files, full_path, target_path, rules):
    """Write a fresh normal DB at exactly `target_path` (must not exist). `rules` is a
    privacy.Rules (pass privacy.Rules() for none). Returns {table: row count}."""
    return normal_db_service.build_normal_db(deps_for(files), full_path, target_path, rules)


def build_normal_atomic(files, full_path, directory, rules, leak_check=True):
    """Build into a temp file in `directory`, leak-check it against the full DB, then replace
    knowledge-normal.db. On any failure the temp file is removed AND a previous knowledge-normal.db is
    removed too (no file is safer than an out-of-date one). Returns (final_path, counts)."""
    return normal_db_service.build_normal_atomic(deps_for(files), full_path, directory, rules, leak_check)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: normal_db.py FULL_DB OUT_DIR   (builds OUT_DIR/knowledge-normal.db)", file=sys.stderr)
        return 2
    try:
        import local_buildfiles
        import paths
        import privacy_store
        rules = privacy_store.load_rules(paths.load().privacy_rules())
        path, counts = build_normal_atomic(local_buildfiles, argv[0], argv[1], rules)
    except Exception as e:
        print(f"error: normal DB build failed: {e}", file=sys.stderr)
        return 1
    print(f"Built {path}\n{format_counts(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
