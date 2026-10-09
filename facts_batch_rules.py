"""Batch add_facts, the rules (domain: no file, db, git, clock or mode state).

Result types, the per-item parsing and the duplicate test. `facts_batch` is the use case: it reads
the facts file and the mode, resolves privacy, writes through `add_fact_store` and commits.
"""
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from new_fact import NewFact

MAX_BATCH = 1000
DEFAULT_TRUST = "unverified"   # a fact the user stated in chat: not independently verified
DEFAULT_CAPTURED_VIA = "register-facts"

_STR_KEYS = ("statement", "subject", "trust_level", "domain", "visibility", "kind", "valid_from", "valid_to", "applies_to", "trust_rationale", "notes",
             "recheck_by", "recheck_rationale", "source_citekey", "source_locator", "source_quote")
_BOOL_KEYS = ("is_original_claim", "is_personal", "no_decay")
ITEM_KEYS = frozenset(_STR_KEYS + _BOOL_KEYS)


@dataclass
class ItemResult:
    index: int                                  # position in the input list
    outcome: str                                # saved | would_save | invalid | not_saved | duplicate | refused
    statement: Optional[str] = None
    subject: Optional[str] = None
    source_key: Optional[str] = None            # set when saved / would_save (dry run: not stored)
    visibility: Optional[str] = None            # the RESOLVED value that is (or would be) stored
    requested: Optional[str] = None             # what the item/session asked for: an input only
    raised: bool = False                        # stored private although the request was not
    rule: Optional[str] = None                  # privacy.Resolution.explain(): the rule(s) behind `visibility`
    errors: List[str] = field(default_factory=list)  # why invalid / refused / duplicate / not_saved

    @property
    def stored(self):
        return self.outcome == "saved"


@dataclass
class BatchResult:
    items: List[ItemResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)   # batch-level: nothing was written
    notes: List[str] = field(default_factory=list)
    dry_run: bool = False
    total: Optional[int] = None                       # facts in general_facts.json afterwards
    commit: Optional[str] = None
    commit_error: Optional[str] = None                # facts written but NOT committed
    detached: bool = False

    @property
    def saved(self):
        return [i for i in self.items if i.outcome == "saved"]

    @property
    def ok(self):
        """True when nothing went wrong: no batch error, no failed commit, and every item saved,
        would-save or an exact duplicate of a fact already there."""
        return (not self.errors and self.commit_error is None
                and all(i.outcome in ("saved", "would_save", "duplicate") for i in self.items))

    def to_dict(self):
        d = asdict(self)
        d["ok"] = self.ok
        return d


def norm(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


def parse_item(raw):
    """(item_fields, errors). item_fields is None when the item is not usable at all."""
    if not isinstance(raw, dict):
        return None, [f"item must be an object, got {type(raw).__name__}"]
    errors = []
    unknown = sorted(str(k) for k in raw if k not in ITEM_KEYS)
    if unknown:
        errors.append(f"unknown key(s) {unknown}; allowed: {sorted(ITEM_KEYS)} (source_key, status and provenance are set by the batch)")
    for key in ("statement", "subject"):
        if raw.get(key) is None:
            errors.append(f"{key} is required")
    for key in _STR_KEYS:
        if raw.get(key) is not None and not isinstance(raw[key], str):
            errors.append(f"{key} must be a string")
    for key in _BOOL_KEYS:
        if raw.get(key) is not None and not isinstance(raw[key], bool):
            errors.append(f"{key} must be true or false")
    return raw, errors


def build_fact(fields, requested_default, captured_via, session_id, status):
    """The NewFact for a parsed item (its keys already checked by parse_item)."""
    return NewFact(
        statement=fields["statement"], subject=fields["subject"],
        trust_level=fields.get("trust_level") or DEFAULT_TRUST,
        no_decay=bool(fields.get("no_decay", False)),
        domain=fields.get("domain") or "general",
        is_original_claim=bool(fields.get("is_original_claim", False)),
        is_personal=bool(fields.get("is_personal", True)),
        visibility=fields.get("visibility") or requested_default,
        kind=fields.get("kind") or "unclassified",
        valid_from=fields.get("valid_from"), valid_to=fields.get("valid_to"),
        applies_to=fields.get("applies_to"),
        trust_rationale=fields.get("trust_rationale"), notes=fields.get("notes"),
        recheck_by=fields.get("recheck_by"), recheck_rationale=fields.get("recheck_rationale"),
        source_citekey=fields.get("source_citekey"), source_locator=fields.get("source_locator"),
        source_quote=fields.get("source_quote"),
        captured_via=captured_via, session_id=session_id, status=status)


def existing_keys(entries):
    """{(subject, normalized statement)} of the usable entries in the facts file."""
    return {(e.get("subject"), norm(e["statement"])) for e in entries
            if isinstance(e, dict) and isinstance(e.get("subject"), str) and isinstance(e.get("statement"), str)}


def duplicate_reason(existing, entry):
    """The reason `entry` repeats one of `existing` (the file's records), or None. Runs inside the lock."""
    key = (entry["subject"], norm(entry["statement"]))
    for e in existing:
        if isinstance(e, dict) and isinstance(e.get("statement"), str) and (e.get("subject"), norm(e["statement"])) == key:
            return "already present (same subject and statement)"
    return None
