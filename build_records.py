"""The plain values a build step and the build repositories exchange: the core's (sources, subjects, facts,
revisions, privacy, entities). The optional client sources' records (music, measurements, claims) are owned by
`client/`: `client.music_records`, `client.measurements_records`, `client.claims_port`. Nothing here may name
them: the core never imports `client`.

Domain service ring: frozen dataclasses and identifier types, no I/O, no database handle. A repository
(`build_repositories_port`) accepts and returns only these (and the model's own values: `subject_rules.Subject`,
`entity_rules.Entity`, `privacy.Rules`), so a step never sees a cursor, a row or a SQL string.

The field names are the column names of schema.sql on purpose: the adapter maps a record to its table 1:1 and the
characterization tests (tests/test_build_characterization.py) compare against rows. A field a step does not have
is `None`, which the adapter stores as NULL exactly as the step's own INSERT does today (the nullable columns of
`sources` and `facts` have no non-NULL default, so an omitted column and an explicit None are the same row).
"""
from dataclasses import dataclass, field
from typing import Mapping, NewType, Optional, Tuple

SourceId = NewType("SourceId", int)
SubjectId = NewType("SubjectId", int)
FactId = NewType("FactId", int)
VaultFileId = NewType("VaultFileId", int)
EntityRowId = NewType("EntityRowId", int)


# ----------------------------------------------------------------------------------------------- sources

@dataclass(frozen=True)
class NewSource:
    """One `sources` row to insert. `publisher` is a name: the repository gets-or-creates it before inserting the
    row (so a new publisher's id is allocated first, as today). Authors are linked separately with
    `SourceRepository.link_author` after the identifiers, which is the order the steps use now."""
    citekey: Optional[str]
    name: str
    source_type: str
    publisher: Optional[str] = None
    url: Optional[str] = None
    published_date: Optional[str] = None
    retrieved_date: Optional[str] = None
    description: Optional[str] = None
    origin_path: Optional[str] = None
    status: str = "active"
    status_date: Optional[str] = None
    status_note: Optional[str] = None
    edition: Optional[str] = None
    original_published_date: Optional[str] = None
    acquired_at: Optional[str] = None
    acquired_via: Optional[str] = None
    where_from: Optional[str] = None
    acquired_note: Optional[str] = None
    content_sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    file_mtime: Optional[str] = None
    mime_type: Optional[str] = None
    file_state: Optional[str] = None


@dataclass(frozen=True)
class IdentifierOwner:
    """The source that already holds an identifier (scheme, value)."""
    source_id: SourceId
    citekey: Optional[str]


@dataclass(frozen=True)
class IdentifierConflict:
    """An identifier a second source also claims: recorded, reported, never merged."""
    scheme: str
    value: str
    owner_citekey: Optional[str]


@dataclass(frozen=True)
class VaultFileState:
    """A `vault_files` row as far as identity needs it: `file_state` None means not yet fingerprinted this build."""
    id: VaultFileId
    file_state: Optional[str]


@dataclass(frozen=True)
class FileFixity:
    """What is recorded about a vault file the first time a build sees it: its fingerprint (inputs.fingerprint:
    content_sha256, size_bytes, file_mtime, mime_type, file_state) and custodial history (inputs.acquisition:
    acquired_at, acquired_via, where_from, acquired_note)."""
    content_sha256: Optional[str]
    size_bytes: Optional[int]
    file_mtime: Optional[str]
    mime_type: Optional[str]
    file_state: Optional[str]
    acquired_at: Optional[str] = None
    acquired_via: Optional[str] = None
    where_from: Optional[str] = None
    acquired_note: Optional[str] = None


@dataclass(frozen=True)
class NewSourceRelation:
    source_id: SourceId
    relation: str
    related_source_id: SourceId


# ----------------------------------------------------------------------------------------------- facts

@dataclass(frozen=True)
class NewFact:
    """One `facts` row to insert (the ingest path: provided_by 'user', last_reviewed_at = date_added)."""
    subject_id: SubjectId
    statement: str
    is_original_claim: int
    is_personal: int
    trust_level: str
    trust_rationale: Optional[str]
    date_added: str
    visibility: str
    status: str
    source_key: str
    freshness: str
    kind: str
    notes: Optional[str] = None
    recheck_by: Optional[str] = None
    recheck_rationale: Optional[str] = None
    origin_file_id: Optional[VaultFileId] = None
    captured_via: Optional[str] = None
    session_id: Optional[str] = None
    captured_at: Optional[str] = None
    source_quote: Optional[str] = None
    extracted_from_sha256: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    applies_to: Optional[str] = None
    provided_by: str = "user"


@dataclass(frozen=True)
class FactSourceLink:
    """`fact_sources`: the fact cites the source (INSERT OR IGNORE: a repeated pair is not an error)."""
    fact_id: FactId
    source_id: SourceId
    locator: Optional[str] = None
    quote: Optional[str] = None


# -------------------------------------------------------------------------------------- revisions, privacy

@dataclass(frozen=True)
class RevisionWriteBack:
    """What the latest log revision of a fact writes into its `facts` row (apply_fact_revisions)."""
    fact_id: FactId
    record: Mapping[str, object]
    superseded_by_fact_id: Optional[FactId]


@dataclass(frozen=True)
class FirstRevision:
    """Revision 1 of a fact as the log validator needs it: when it was made, its freshness and the fields a
    legacy log line may inherit (revisions.LEGACY_INHERITED)."""
    changed_at: str
    freshness: Optional[str]
    inherited: Mapping[str, object]


@dataclass(frozen=True)
class PrivacyOutcome:
    """`privacy_store.apply_rules_to_db`'s result: facts raised to private, as (fact id, explanation), and how
    many subjects now carry the private flag."""
    raised: Tuple[Tuple[FactId, str], ...]
    private_subjects: int


# --------------------------------------------------------------------------------------------- entities

@dataclass(frozen=True)
class FactText:
    """The text of one fact the entity matcher scans (the same text the privacy resolver scans)."""
    fact_id: FactId
    texts: Tuple[Optional[str], ...]
