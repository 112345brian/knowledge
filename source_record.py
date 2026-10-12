"""A source as one value: what the build knows about a citable work before it has a database id.

Model ring: imports only other model modules and does no I/O. The readers (`ingest.source_readers`) build these from
manual_sources.json and the vault's citation notes; a `SourceRepository` stores them. Everything a record holds is
already validated: status fields (`SourceState`), custodial history, identifiers (malformed ones are set aside in
`identifier_problems`, never dropped silently) and relations.
"""
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

import acquisition_rules
import fixity_rules
from source_status import Relation, SourceState

NO_ACQUISITION = {k: None for k in acquisition_rules.COLUMNS}


@dataclass(frozen=True)
class SourceRecord:
    citekey: Optional[str]
    name: str
    source_type: str
    state: SourceState = SourceState()
    publisher: Optional[str] = None
    url: Optional[str] = None
    published_date: Optional[str] = None
    retrieved_date: Optional[str] = None
    description: Optional[str] = None
    origin_path: Optional[str] = None
    # custodial history (acquisition_rules.COLUMNS); the file fingerprint (fixity_rules.FINGERPRINT_COLUMNS) is None
    # for a source that is not a file
    acquisition: Mapping[str, Optional[str]] = field(default_factory=lambda: dict(NO_ACQUISITION))
    fingerprint: Optional[Mapping[str, object]] = None
    # (byline position, name): a blank name in the byline leaves a gap in the positions, as it always has
    authors: Tuple[Tuple[int, str], ...] = ()
    identifiers: Tuple[Tuple[str, str], ...] = ()
    identifier_problems: Tuple[str, ...] = ()
    relations: Tuple[Relation, ...] = ()

    def __post_init__(self):
        if self.fingerprint is not None and set(self.fingerprint) != set(fixity_rules.FINGERPRINT_COLUMNS):
            raise ValueError(f"source {self.citekey!r}: fingerprint must have exactly {fixity_rules.FINGERPRINT_COLUMNS}")
        if set(self.acquisition) != set(acquisition_rules.COLUMNS):
            raise ValueError(f"source {self.citekey!r}: acquisition must have exactly {acquisition_rules.COLUMNS}")


@dataclass(frozen=True)
class IdentifierConflict:
    """An identifier a second source claimed: kept as a conflict with its owner, never merged."""
    scheme: str
    value: str
    owner: Optional[str]   # the owning source's citekey
