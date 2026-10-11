"""The decisions of the fact ingest (pilot/batch files and general_facts.json), domain: no file, no db.

Both fact steps used to repeat these inline; they are one implementation here, and `fact_ingest_service` runs it
against the fact repository port. What lives here:

    EntryRef / entry_refs     an entry of a data file with its source key and where it came from
    screen                    Skipped (a warning or silent) or Screened: subject, statement, trust, visibility, status, key
    plan_fact                 everything a loadable entry says about its row: personal flag, origin path, freshness,
                              kind, validity, applies_to, date_added, the implicit revision 1 -- or the build error
    FactRow                   the `facts` columns an insert writes (the ids are filled in by the service)
    IngestReport/render       what a step returns and the lines it prints

The two steps differ only in an `IngestProfile` (how subjects are found, whether the origin note and the personal
flag are derived or taken from the entry, whether the report carries the breakdown counts).
"""
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

import fixity_rules
import revisions
from ingest import fact_ingest_rules
from ingest.fact_ingest_rules import Hints, NO_HINTS, Skip
from locations import FACTS_FILENAME

PILOT_FILENAME = "pilot_facts.json"
FACT_FILENAMES = tuple(name for name, _ in revisions.ENTRY_FILES if name != FACTS_FILENAME)
BREAKDOWNS = ("freshness", "trust_level", "is_personal")   # the facts columns the fact-files report counts by


@dataclass(frozen=True)
class IngestProfile:
    """What differs between the fact-file step and the general-facts step."""
    label: str                              # the report's first line is `[label] inserted N, skipped M (bad shape)`
    subject_domain: Optional[str] = None    # None: a new subject gets the schema's domain; else the entry's `domain`, or this
    resolve_aliases: bool = False           # an entry filed under a subject alias belongs to the canonical subject (#43)
    derive_personal: bool = True            # heuristic is_personal (True) or the entry's own `is_personal`, default true
    derive_origin: bool = True              # the origin note from `notes` and the vault (True) or only `origin_path`
    breakdowns: bool = True                 # the report ends with the counts by freshness / trust / is_personal


FACT_FILES = IngestProfile(label="facts")
GENERAL_FACTS = IngestProfile(label="seed_general_facts", subject_domain="general", resolve_aliases=True,
                              derive_personal=False, derive_origin=False, breakdowns=False)


@dataclass(frozen=True)
class EntryRef:
    """One entry of a data file: the entry itself, the file and index it came from, and its source key (its own,
    or the deterministic legacy-... key from revisions.derive_keys until backfill_source_keys.py writes one)."""
    item: Mapping
    key: str
    file: str
    index: int
    is_pilot: bool = False

    @property
    def where(self) -> Tuple[str, int]:
        return self.file, self.index

    @property
    def has_own_key(self) -> bool:
        return bool(self.item.get("source_key"))


def entry_refs(items, file, is_pilot=False) -> List[EntryRef]:
    """Every entry of the parsed array `items` of `file`, with its key."""
    return [EntryRef(item, key, file, index, is_pilot)
            for index, (item, key) in enumerate(zip(items, revisions.derive_keys(items, file)))]


@dataclass(frozen=True)
class Skipped:
    """An entry that is not loaded; `warning` is printed (None = a silent skip)."""
    entry: EntryRef
    warning: Optional[str]


@dataclass(frozen=True)
class Screened:
    entry: EntryRef
    subject: str
    statement: str
    trust: str
    visibility: str
    status: str
    key: str            # checked well formed


def screen(entry: EntryRef):
    """`Screened` for a loadable entry, `Skipped` for one that is not (see fact_ingest_rules.screen_item). A source
    key that is not well formed is a ValueError: it fails the build."""
    try:
        subject, statement, trust, visibility, status = fact_ingest_rules.screen_item(entry.item)
    except Skip as skip:
        return Skipped(entry, skip.warning)
    key = fact_ingest_rules.check_source_key(entry.key, statement)
    return Screened(entry, subject, statement, trust, visibility, status, key)


def duplicate_key_error(screened: Screened) -> ValueError:
    return ValueError(f"duplicate source_key {screened.key!r} (fact {screened.statement[:60]!r})")


@dataclass(frozen=True)
class FactRow:
    """The columns of one `facts` insert. `subject_id` and `origin_file_id` are resolved by the service (they need
    the repository); everything else is decided here."""
    statement: str
    is_original_claim: int
    is_personal: int
    trust_level: str
    trust_rationale: Optional[str]
    date_added: str
    last_reviewed_at: str
    notes: Optional[str]
    recheck_by: Optional[str]
    recheck_rationale: Optional[str]
    visibility: str
    captured_via: Optional[str]
    session_id: Optional[str]
    captured_at: Optional[str]
    source_quote: Optional[str]
    status: str
    source_key: str
    freshness: str
    extracted_from_sha256: Optional[str]
    kind: str
    valid_from: Optional[str]
    valid_to: Optional[str]
    applies_to: Optional[str]
    provided_by: str = "user"
    subject_id: Optional[int] = None
    origin_file_id: Optional[int] = None


@dataclass(frozen=True)
class FactPlan:
    """A screened entry, decided: the subject and domain to resolve, the origin note to fingerprint, the row, its
    implicit revision 1 and the source it cites."""
    screened: Screened
    subject: str
    domain: Optional[str]
    origin_path: Optional[str]
    row: FactRow
    revision: Dict[str, Any]
    citekey: Optional[str]
    locator: Optional[str]
    quote: Optional[str]


def plan_fact(screened: Screened, profile: IngestProfile, hints: Hints = NO_HINTS, notes_vault=None) -> FactPlan:
    """Everything the entry says about its row, or the build error (invalid baseline hash, missing/invalid
    date_added #35, freshness #7, kind #39, validity #40, applies_to #45). No side effect."""
    entry, item = screened.entry, screened.entry.item
    statement = screened.statement
    is_original = 1 if item.get("is_original_claim") else 0
    if profile.derive_personal:
        is_personal = fact_ingest_rules.classify_is_personal(statement, item.get("notes"), is_original,
                                                             item.get("measurement_metric_link"), hints)
    else:
        is_personal = 1 if item.get("is_personal", True) else 0
    origin_path = item.get("origin_path")
    if profile.derive_origin and not origin_path:
        origin_path = fact_ingest_rules.resolve_origin_path(item.get("notes"), notes_vault, hints)

    baseline = item.get("extracted_from_sha256")
    if baseline is not None and not fixity_rules.valid_sha256(baseline):
        raise ValueError(f"invalid extracted_from_sha256 {baseline!r} on fact {statement[:60]!r} (want 64 lowercase hex)")
    date_added = fact_ingest_rules.require_date_added(item, entry.file, entry.index)
    # #7: the entry's own freshness, or for a legacy entry (original files, no provenance) 'recheck' if it has a
    # recheck_by, else 'unreviewed' plus the "predates this field" note. Anything else fails the build.
    eff = revisions.effective_entry(item, entry.file)
    valid_from, valid_to = revisions.entry_validity(item)
    row = FactRow(
        statement=statement, is_original_claim=is_original, is_personal=is_personal, trust_level=screened.trust,
        trust_rationale=item.get("trust_rationale"), date_added=date_added, last_reviewed_at=date_added,
        notes=eff.get("notes"), recheck_by=item.get("recheck_by"), recheck_rationale=item.get("recheck_rationale"),
        visibility=screened.visibility, captured_via=item.get("captured_via"), session_id=item.get("session_id"),
        captured_at=item.get("captured_at"), source_quote=item.get("source_quote"), status=screened.status,
        source_key=screened.key, freshness=eff["freshness"], extracted_from_sha256=baseline,
        kind=revisions.entry_kind(item), valid_from=valid_from, valid_to=valid_to,
        applies_to=revisions.entry_applies_to(item))
    domain = (item.get("domain") or profile.subject_domain) if profile.subject_domain else None
    return FactPlan(screened, screened.subject, domain, origin_path, row,
                    revisions.implicit_revision(screened.key, item, date_added, entry.file),
                    item.get("source_citekey") or None, item.get("source_locator"), item.get("source_quote"))


def with_ids(row: FactRow, subject_id, origin_file_id) -> FactRow:
    return dataclasses.replace(row, subject_id=subject_id, origin_file_id=origin_file_id)


@dataclass(frozen=True)
class IngestedFact:
    """An entry that became a row: its fact id, key, and the source it was linked to (None: no citation, or the
    citekey is unknown)."""
    fact_id: int
    source_key: str
    subject_id: int
    source_id: Optional[int] = None


@dataclass
class IngestReport:
    """What one ingest step did, in the order it did it. Rendered to printed lines by `render`."""
    label: str
    ingested: List[IngestedFact] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)
    unknown_citekeys: set = field(default_factory=set)
    derived_keys: int = 0
    privacy_raised: int = 0
    breakdowns: Dict[str, Dict[Any, int]] = field(default_factory=dict)


def render(report: IngestReport) -> List[str]:
    """The lines a step prints for `report`."""
    lines = [s.warning for s in report.skipped if s.warning]
    if report.privacy_raised:
        lines.append(f"  privacy rules raised {report.privacy_raised} fact(s) to private")
    lines.append(f"[{report.label}] inserted {len(report.ingested)}, skipped {len(report.skipped)} (bad shape)")
    if report.unknown_citekeys:
        lines.append(f"  WARNING -- citekeys referenced but not found in sources: {sorted(report.unknown_citekeys)}")
    if report.derived_keys:
        lines.append(f"  note: {report.derived_keys} entries have no source_key yet; used deterministic legacy-* keys. "
                     f"Run backfill_source_keys.py to write them into the files (same values).")
    for column, counts in report.breakdowns.items():
        lines.append(f"  facts by {column}: {counts}")
    return lines
