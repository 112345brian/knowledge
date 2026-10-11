"""Adapter for `fact_ingest_port.FactRepository`: the SQL of the fact ingest over the build's connection.

The rows it writes are `facts`, `fact_sources` and `fact_revisions`; the sources it looks up are read by citekey. The decisions are not here
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

def source_ids_by_citekey(con):
    return {r[0]: r[1] for r in con.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}


def fact_exists(con, source_key):
    return con.execute("SELECT 1 FROM facts WHERE source_key = ?", (source_key,)).fetchone() is not None


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
