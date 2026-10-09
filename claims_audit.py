"""Stale-premise audit for claims (#1). Domain (pure): the staleness rules. `claims_store` reads the db and the clock and calls these;
the `knowledge.py audit-claims` CLI wraps that.

A claim is an argument; the facts it cites (claim_facts) are its premises. A premise is stale when
  - superseded: facts.status = 'superseded'
  - retracted:  facts.status = 'retracted'
  - past_recheck_by: the fact is still active but its recheck_by is a date strictly before `today`.
Only ISO dates are compared (YYYY-MM-DD, optionally followed by a time; YYYY-MM means the end of that
month, YYYY the end of that year). recheck_by is free text elsewhere ("next panel", "after surgery"),
and text that is not an ISO date is never flagged: it is returned by `claims_store.unparseable_rechecks()` so the
caller can surface it instead of silently skipping it.

`claims_store.audit_claims()` never writes. A claim with no facts, or whose facts are all active and current, yields
no rows. A fact cited by several claims yields one row per claim.
"""
import calendar
import re
from datetime import date

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


def stale_reason(status, recheck_by, today):
    """Why a cited fact is a stale premise as of the date `today`, or None if it is not stale."""
    if status in ("superseded", "retracted"):
        return status
    due = parse_recheck_by(recheck_by)
    if due is None or due >= today:
        return None
    return "past_recheck_by"


def finding(row, reason):
    """The audit row for one (claim, stale premise). `row` has the audit query's columns."""
    return {
        "claim_id": row["claim_id"], "claim_statement": row["claim_statement"],
        "inference_type": row["inference_type"], "fact_id": row["fact_id"],
        "fact_statement": row["fact_statement"], "reason": reason, "recheck_by": row["recheck_by"],
        "superseded_by_fact_id": row["superseded_by_fact_id"],
        "trust_rationale": row["trust_rationale"], "notes": row["notes"]}


def unparseable(rows):
    """[{fact_id, recheck_by}] for (fact_id, recheck_by) pairs whose text is not an ISO date."""
    return [{"fact_id": i, "recheck_by": t} for i, t in rows if parse_recheck_by(t) is None]
