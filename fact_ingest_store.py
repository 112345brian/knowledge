"""Adapter for `fact_ingest_port.FactRepository`: the SQL of the fact ingest over the build's connection.

The rows it writes are `facts`, `fact_sources`, `fact_revisions` and `vault_files`; the subjects and sources it
looks up are read as the identity operations of the fact repository. The decisions are not here
(`fact_ingest_decisions`); this module only reads and writes what it is told to.
"""
import dataclasses

import fact_ingest_decisions as decisions
import revisions_store

# One named parameter per `fact_ingest_decisions.FactRow` field (a test pins the two lists equal).
_INSERT_FACT = """INSERT INTO facts (subject_id, statement, is_original_claim, is_personal, trust_level, trust_rationale,
        provided_by, date_added, last_reviewed_at, notes, recheck_by, recheck_rationale, origin_file_id, visibility,
        captured_via, session_id, captured_at, source_quote, status, source_key, freshness,
        extracted_from_sha256, kind, valid_from, valid_to, applies_to)
    VALUES (:subject_id, :statement, :is_original_claim, :is_personal, :trust_level, :trust_rationale,
        :provided_by, :date_added, :last_reviewed_at, :notes, :recheck_by, :recheck_rationale, :origin_file_id, :visibility,
        :captured_via, :session_id, :captured_at, :source_quote, :status, :source_key, :freshness,
        :extracted_from_sha256, :kind, :valid_from, :valid_to, :applies_to)"""
_FINGERPRINT_UPDATE = (
    "UPDATE vault_files SET content_sha256 = :content_sha256, size_bytes = :size_bytes, file_mtime = :file_mtime, "
    "mime_type = :mime_type, file_state = :file_state, acquired_at = :acquired_at, acquired_via = :acquired_via, "
    "where_from = :where_from, acquired_note = :acquired_note WHERE id = :id")


def subject_id(con, name, domain=None, resolve_aliases=False):
    row = con.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
    if row is None and resolve_aliases:   # #43: an entry filed under an alias belongs to the canonical subject
        row = con.execute("SELECT subject_id FROM subject_aliases WHERE alias = ?", (name,)).fetchone()
    if row:
        return row[0]
    if domain is None:
        return con.execute("INSERT INTO subjects (name) VALUES (?)", (name,)).lastrowid
    return con.execute("INSERT INTO subjects (name, domain) VALUES (?, ?)", (name, domain)).lastrowid


def source_ids_by_citekey(con):
    return {r[0]: r[1] for r in con.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}


def fact_exists(con, source_key):
    return con.execute("SELECT 1 FROM facts WHERE source_key = ?", (source_key,)).fetchone() is not None


def find_vault_file(con, path):
    row = con.execute("SELECT id, file_state FROM vault_files WHERE path = ?", (path,)).fetchone()
    return None if row is None else (row[0], row[1] is not None)


def add_vault_file(con, path):
    return con.execute("INSERT INTO vault_files (path) VALUES (?)", (path,)).lastrowid


def record_vault_file(con, file_id, fingerprint, acquisition):
    con.execute(_FINGERPRINT_UPDATE, {**fingerprint, **acquisition, "id": file_id})


def insert_fact(con, row):
    return con.execute(_INSERT_FACT, dataclasses.asdict(row)).lastrowid


def insert_revision(con, fact_id, revision):
    revisions_store.insert_revision_row(con, fact_id, revision)


def link_source(con, fact_id, source_id, locator, quote):
    con.execute("INSERT OR IGNORE INTO fact_sources (fact_id, source_id, locator, quote) VALUES (?, ?, ?, ?)",
                (fact_id, source_id, locator, quote))


def facts_by(con, column):
    if column not in decisions.BREAKDOWNS:
        raise ValueError(f"cannot count facts by {column!r}; one of {list(decisions.BREAKDOWNS)}")
    return dict(con.execute(f"SELECT {column}, COUNT(*) FROM facts GROUP BY {column}").fetchall())


def commit(con):
    con.commit()
