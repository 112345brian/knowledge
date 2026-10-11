"""Adapter for claims: the stale-premise audit (#1) and the claims repository the build seeds through.

The audit reads claims and facts from the db and applies the rules in `claims_audit` (the domain); it never writes.
The `knowledge.py audit-claims` CLI wraps it. `insert_claim` and `link_claim_premise` are the repository's only
writes: they store what `claims_rules` already normalized, and `link_claim_premise` leaves premise matching to
SQLite's LIKE (see `claims_rules`).

A fact cited by several claims yields one row per claim. A claim with no facts, or whose facts are
all active and current, yields no rows.
"""
import sqlite3
from datetime import date

import claims_audit
import claims_rules


def _today(today):
    return date.fromisoformat(today) if isinstance(today, str) else today


def audit_claims(db, today):
    """Return a list of dicts, ordered by claim_id then fact_id, one per (claim, stale premise):
    claim_id, claim_statement, inference_type, fact_id, fact_statement, reason, recheck_by,
    superseded_by_fact_id, trust_rationale, notes, plus role (grounds | backing | rebuttal), severity
    ("weakens" for grounds/backing, "info" for a rebuttal, whose reason is prefixed "rebuttal_") and the link's
    note (why the fact is cited). `db` is an open sqlite3 connection; `today` is a date or an ISO date string."""
    today = _today(today)
    old_factory, db.row_factory = db.row_factory, sqlite3.Row
    try:
        rows = db.execute(
            """SELECT c.id AS claim_id, c.statement AS claim_statement, c.inference_type,
                      f.id AS fact_id, f.statement AS fact_statement, f.status, f.recheck_by,
                      f.superseded_by_fact_id, f.trust_rationale, f.notes, cf.role, cf.note AS link_note
               FROM claims c JOIN claim_facts cf ON cf.claim_id = c.id JOIN facts f ON f.id = cf.fact_id
               ORDER BY c.id, f.id""").fetchall()
    finally:
        db.row_factory = old_factory
    out = []
    for r in rows:
        reason = claims_audit.stale_reason(r["status"], r["recheck_by"], today)
        if reason is not None:
            out.append(claims_audit.finding(r, reason))
    return out


def unparseable_rechecks(db):
    """Active cited facts whose recheck_by is set but is not an ISO date (never flagged by the audit)."""
    rows = db.execute(
        """SELECT DISTINCT f.id, f.recheck_by FROM claim_facts cf JOIN facts f ON f.id = cf.fact_id
           WHERE f.status = 'active' AND f.recheck_by IS NOT NULL AND TRIM(f.recheck_by) != ''
           ORDER BY f.id""").fetchall()
    return claims_audit.unparseable(rows)


def insert_claim(cur, claim):
    """Store a `claims_rules.Claim` (not its premises) and return the new claim id."""
    cur.execute("INSERT INTO claims (statement, notes, inference_type, warrant, qualifier) VALUES (?, ?, ?, ?, ?)",
                (claim.statement, claim.notes, claim.inference_type, claim.warrant, claim.qualifier))
    return cur.lastrowid


def link_claim_premise(cur, claim_id, premise):
    """Link `claim_id` to every fact whose statement matches the premise's prefix; return how many were linked.

    The match is SQLite's `statement LIKE prefix%`, left to the database on purpose: ASCII case-insensitive, `%` and `_`
    in the prefix are wildcards (`claims_rules` documents the policy). A fact may match several premises of one
    claim; each match is its own claim_facts row, as before.
    """
    cur.execute(
        "INSERT INTO claim_facts (claim_id, fact_id, role, note) SELECT ?, id, ?, ? FROM facts WHERE statement LIKE ?",
        (claim_id, premise.role, premise.note, claims_rules.match_pattern(premise.prefix)))
    return cur.rowcount
