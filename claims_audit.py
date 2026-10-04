"""Stale-premise audit for claims (#1). Library only; the `knowledge.py audit-claims` CLI wraps this.

A claim is an argument; the facts it cites (claim_facts) are its premises. A premise is stale when
  - superseded: facts.status = 'superseded'
  - retracted:  facts.status = 'retracted'
  - past_recheck_by: the fact is still active but its recheck_by is a date strictly before `today`.
Only ISO dates are compared (YYYY-MM-DD, optionally followed by a time; YYYY-MM means the end of that
month, YYYY the end of that year). recheck_by is free text elsewhere ("next panel", "after surgery"),
and text that is not an ISO date is never flagged: it is returned by unparseable_rechecks() so the
caller can surface it instead of silently skipping it.

audit_claims() never writes. A claim with no facts, or whose facts are all active and current, yields
no rows. A fact cited by several claims yields one row per claim.
"""
import calendar
import re
import sqlite3
from datetime import date

import clock

REASONS = ("superseded", "retracted", "past_recheck_by")
_ISO = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?(?:[T ].*)?$")


def parse_recheck_by(text):
    """Return the last date on which `text` still counts as not-yet-due, or None if not an ISO date."""
    if text is None:
        return None
    m = _ISO.match(text.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    try:
        if mo is None:
            return date(y, 12, 31)
        if d is None:
            return date(y, int(mo), calendar.monthrange(y, int(mo))[1])
        return date(y, int(mo), int(d))
    except ValueError:  # month 13, Feb 30, year 0
        return None


def _today(today):
    if today is None:
        return clock.now().date()
    return date.fromisoformat(today) if isinstance(today, str) else today


def audit_claims(db, today=None):
    """Return a list of dicts, ordered by claim_id then fact_id, one per (claim, stale premise):
    claim_id, claim_statement, inference_type, fact_id, fact_statement, reason, recheck_by,
    superseded_by_fact_id, trust_rationale, notes. `db` is an open sqlite3 connection."""
    today = _today(today)
    old_factory, db.row_factory = db.row_factory, sqlite3.Row
    try:
        rows = db.execute(
            """SELECT c.id AS claim_id, c.statement AS claim_statement, c.inference_type,
                      f.id AS fact_id, f.statement AS fact_statement, f.status, f.recheck_by,
                      f.superseded_by_fact_id, f.trust_rationale, f.notes
               FROM claims c JOIN claim_facts cf ON cf.claim_id = c.id JOIN facts f ON f.id = cf.fact_id
               ORDER BY c.id, f.id""").fetchall()
    finally:
        db.row_factory = old_factory
    out = []
    for r in rows:
        if r["status"] in ("superseded", "retracted"):
            reason = r["status"]
        else:
            due = parse_recheck_by(r["recheck_by"])
            if due is None or due >= today:
                continue
            reason = "past_recheck_by"
        out.append({
            "claim_id": r["claim_id"], "claim_statement": r["claim_statement"],
            "inference_type": r["inference_type"], "fact_id": r["fact_id"],
            "fact_statement": r["fact_statement"], "reason": reason, "recheck_by": r["recheck_by"],
            "superseded_by_fact_id": r["superseded_by_fact_id"],
            "trust_rationale": r["trust_rationale"], "notes": r["notes"]})
    return out


def unparseable_rechecks(db):
    """Active cited facts whose recheck_by is set but is not an ISO date (never flagged by the audit)."""
    rows = db.execute(
        """SELECT DISTINCT f.id, f.recheck_by FROM claim_facts cf JOIN facts f ON f.id = cf.fact_id
           WHERE f.status = 'active' AND f.recheck_by IS NOT NULL AND TRIM(f.recheck_by) != ''
           ORDER BY f.id""").fetchall()
    return [{"fact_id": i, "recheck_by": t} for i, t in rows if parse_recheck_by(t) is None]
