"""Claim normalization and the premise-matching policy for seeding claims (#44). Domain (pure): no db, file or clock.

A raw claim (from the private claims.json) carries `statement`, `notes`, optional `inference_type`, `warrant` and
`qualifier`, and `fact_match_prefixes`: each entry a statement prefix (a plain string: the fact is `grounds`) or a
dict {"prefix", "role", "note"}. `normalize_claim` turns one into a `Claim` of validated values; the repository
adapter (`claims_store`) writes it. Blank warrant, qualifier and note become None; an unknown role raises.

Matching policy: a premise links every fact whose statement starts with its prefix. The prefix is handed to the
adapter as the SQL LIKE pattern `match_pattern(prefix)`, and the database does the matching, so SQLite's LIKE rules
apply unchanged: ASCII letters match case-insensitively, non-ASCII ones do not, and `%` and `_` inside a prefix are
wildcards, not literals. They are deliberately not escaped here; a plain-string prefix may be empty, and then it
matches every fact. Do not reimplement this in Python: the build's existing claims depend on what LIKE does.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

import claims_audit


@dataclass(frozen=True)
class Premise:
    prefix: str
    role: str
    note: Optional[str]


@dataclass(frozen=True)
class Claim:
    statement: str
    notes: Optional[str]
    inference_type: Optional[str]
    warrant: Optional[str]
    qualifier: Optional[str]
    premises: Tuple[Premise, ...]


def blank_to_none(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, not {type(value).__name__}")
    return value.strip() or None


def premise(entry, claim_statement):
    """The Premise for a plain prefix string or a {"prefix", "role", "note"} dict."""
    if isinstance(entry, str):
        return Premise(entry, "grounds", None)
    if not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str) or not entry["prefix"]:
        raise ValueError(f"claim {claim_statement[:50]!r}: each fact_match_prefixes entry must be a string or a dict with a 'prefix'")
    role = entry.get("role", "grounds")
    if role not in claims_audit.ROLES:
        raise ValueError(f"claim {claim_statement[:50]!r}: role {role!r} must be one of {list(claims_audit.ROLES)}")
    return Premise(entry["prefix"], role, blank_to_none(entry.get("note"), "note"))


def match_pattern(prefix):
    """The SQL LIKE pattern that selects the facts a premise cites (see the module docstring for what that means)."""
    return prefix + "%"


def normalize_claim(raw):
    """A validated Claim from one raw claim dict. Raises ValueError for a bad warrant, qualifier, entry or role."""
    statement = raw["statement"]
    notes = raw["notes"]
    warrant = blank_to_none(raw.get("warrant"), "warrant")
    qualifier = blank_to_none(raw.get("qualifier"), "qualifier")
    premises = tuple(premise(entry, statement) for entry in raw["fact_match_prefixes"])
    return Claim(statement, notes, raw.get("inference_type"), warrant, qualifier, premises)
