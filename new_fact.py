"""A new fact, the rules (domain: no file, db, clock or randomness).

`NewFact` is the shape the CLI, the MCP tools and tests share; `check_fact` is every validation rule
that needs no outside information; `build_entry` turns a checked fact into the JSON entry that
11_seed_general_facts.py expects. The time and the fresh source_key are passed in. The adapter is
`add_fact_store` (locked JSON file, citekey and subject lookups); `add_fact` is the use case and the
`python3 add_fact.py` script.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

import privacy  # noqa: F401  (the AddResult.privacy annotation)
from fact_rules import SOURCE_KEY_RE, VALID_TRUST, VALID_VISIBILITY, VIA_RE
from timestamps import has_offset

# A new fact starts 'pending' (awaiting review, #6) unless the caller already reviewed it
# ('active', e.g. `register facts`, #32). superseded/retracted only arise through revisions.
VALID_NEW_STATUS = ("pending", "active")
SUBJECT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class NewFact:
    """One fact to add. Mirrors the CLI flags so the CLI, the MCP tools and tests share one shape."""
    statement: str
    subject: str
    trust_level: str
    # #7: every new fact needs a recheck_by OR no_decay=True plus a recheck_rationale (neither or
    # both is refused). Freshness is derived from these, never passed in, so a caller cannot
    # supply the legacy-only 'unreviewed'.
    no_decay: bool = False
    domain: str = "general"
    is_original_claim: bool = False
    is_personal: bool = True
    visibility: str = "private"
    trust_rationale: Optional[str] = None
    notes: Optional[str] = None
    recheck_by: Optional[str] = None
    recheck_rationale: Optional[str] = None
    source_citekey: Optional[str] = None
    source_locator: Optional[str] = None
    source_quote: Optional[str] = None
    # Provenance. All optional except that captured_via="mcp" requires session_id and source_quote.
    # captured_at is stamped from the clock when captured_via is set and it is left None.
    captured_via: Optional[str] = None
    session_id: Optional[str] = None
    captured_at: Optional[str] = None
    # Stable identity (#30). Normally left None: build_entry assigns a fresh one.
    source_key: Optional[str] = None
    # Review state (#6). Ad hoc facts start 'pending' and become active via review.approve; a caller
    # that already showed the fact to the user passes 'active'. Always written to the entry.
    status: str = "pending"


@dataclass
class AddResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    entry: Optional[dict] = None
    total: Optional[int] = None
    # #31: how the stored visibility was derived. entry["visibility"] is the resolved value, which
    # may be stricter than NewFact.visibility; privacy.explain() says which rule raised it.
    privacy: Optional["privacy.Resolution"] = None


class DataFileError(Exception):
    """The facts file exists but can't be safely appended to."""


def check_fact(fact):
    """The errors in `fact` that need no outside information (a list, empty when fine). Never prints."""
    errors = []
    if fact.trust_level not in VALID_TRUST:
        errors.append(f"trust_level {fact.trust_level!r} must be one of {sorted(VALID_TRUST)}")
    if not isinstance(fact.visibility, str) or fact.visibility not in VALID_VISIBILITY:
        errors.append(f"visibility {fact.visibility!r} must be one of {sorted(VALID_VISIBILITY)}")
    if not isinstance(fact.status, str) or fact.status not in VALID_NEW_STATUS:
        errors.append(f"status {fact.status!r} must be one of {list(VALID_NEW_STATUS)} for a new fact")
    if not isinstance(fact.no_decay, bool):
        errors.append(f"no_decay {fact.no_decay!r} must be true or false")
    else:
        has_recheck = isinstance(fact.recheck_by, str) and bool(fact.recheck_by.strip())
        if has_recheck and fact.no_decay:
            errors.append("recheck_by and no_decay contradict each other: give a recheck_by (the fact decays) or "
                          "no_decay with a recheck_rationale (it does not), not both")
        elif not has_recheck and not fact.no_decay:
            errors.append("a fact needs freshness: give recheck_by (a date or short phrase), or no_decay together "
                          "with a recheck_rationale saying why it does not decay")
        elif fact.no_decay and not (isinstance(fact.recheck_rationale, str) and fact.recheck_rationale.strip()):
            errors.append("no_decay requires a non-blank recheck_rationale saying why the fact does not decay")
    if not fact.statement.strip():
        errors.append("statement is empty")
    if not SUBJECT_RE.match(fact.subject):
        errors.append(f"--subject {fact.subject!r} must be lowercase kebab-case (e.g. 'car-maintenance')")
    if isinstance(fact.recheck_by, str) and fact.recheck_by.strip() and DATE_RE.match(fact.recheck_by) is None and len(fact.recheck_by) < 4:
        errors.append(f"--recheck-by {fact.recheck_by!r} looks too short to be a date or phrase")
    if fact.source_key is not None and (not isinstance(fact.source_key, str) or not SOURCE_KEY_RE.match(fact.source_key)):
        errors.append(f"source_key {fact.source_key!r} must match {SOURCE_KEY_RE.pattern}")
    if fact.captured_via is not None:
        if not isinstance(fact.captured_via, str) or not VIA_RE.match(fact.captured_via):
            errors.append(f"captured_via {fact.captured_via!r} must be a lowercase kebab-case token (e.g. 'mcp', 'cli', 'migrate-memory')")
    if fact.captured_via is None and (fact.session_id or fact.captured_at):
        errors.append("session_id/captured_at given without captured_via")
    if fact.captured_via == "mcp":
        for name in ("session_id", "source_quote"):
            value = getattr(fact, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name} is required when captured_via is 'mcp'")
    if fact.captured_at is not None and not has_offset(fact.captured_at):
        errors.append(f"captured_at {fact.captured_at!r} must be an ISO-8601 timestamp with a UTC offset (e.g. 2026-10-03T08:00:00+00:00)")
    # A quote needs a citekey to hang off, unless the fact carries provenance: then the quote is
    # the user's own words and lives on the fact itself.
    if fact.source_locator and not fact.source_citekey:
        errors.append("--source-locator/--source-quote given without --source-citekey")
    elif fact.source_quote and not fact.source_citekey and fact.captured_via is None:
        errors.append("--source-locator/--source-quote given without --source-citekey")
    return errors


def build_entry(fact, visibility, source_key, now_iso):
    """The JSON entry 11_seed_general_facts.py expects. Key order is part of the file format.
    `visibility` is the resolved value from privacy.resolve_visibility (None = the caller's request);
    `source_key` is the fresh identity (used unless the fact carries one) and `now_iso` the time."""
    entry = {
        "source_key": fact.source_key or source_key,
        "subject": fact.subject,
        "statement": fact.statement.strip(),
        "trust_level": fact.trust_level,
        "is_original_claim": bool(fact.is_original_claim),
        "is_personal": bool(fact.is_personal),
        "date_added": now_iso,
        "visibility": fact.visibility if visibility is None else visibility,
        "status": fact.status,
        "freshness": "no-decay" if fact.no_decay else "recheck",
    }
    if fact.domain != "general":
        entry["domain"] = fact.domain
    for key in ("trust_rationale", "notes", "recheck_by", "recheck_rationale",
                "source_citekey", "source_locator", "source_quote"):
        value = getattr(fact, key)
        if key == "recheck_by" and isinstance(value, str) and not value.strip():
            continue  # blank counts as absent (a no_decay fact may carry a blank one)
        if value:
            entry[key] = value
    if fact.captured_via:
        entry["captured_via"] = fact.captured_via
        if fact.session_id:
            entry["session_id"] = fact.session_id
        entry["captured_at"] = fact.captured_at or now_iso
    return entry
