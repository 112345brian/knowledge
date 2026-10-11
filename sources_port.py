"""Ports for sources: where normalized source records come from and where they are kept.

`SourceReader` yields the records of one origin (manual_sources.json, the vault's citation notes, or several composed).
`SourceRepository` stores sources and what hangs off them (publishers, authors, identifiers, relations) and the
vault files facts point at; it speaks in records and citekeys, never in SQL or row ids. The SQLite adapter is
`source_repository_store`.
"""
from typing import Dict, List, Mapping, Optional, Protocol, Sequence, Set

from source_record import IdentifierConflict, SourceRecord
from source_status import Relation


class SourceReader(Protocol):
    def records(self) -> List[SourceRecord]:
        """Every source of this origin, validated. Reads the origin afresh on each call."""
        ...

    def relations(self) -> List[Relation]:
        """Only the relations the origin declares, read afresh (a reader that has no cheaper way re-reads and
        validates everything)."""
        ...


class SourceRepository(Protocol):
    def citekeys(self) -> Set[str]: ...
    def add_source(self, record: SourceRecord) -> List[IdentifierConflict]:
        """Store one source with its publisher, authors and identifiers. An identifier already owned by another
        source is recorded as a conflict and returned, not inserted."""
        ...

    def add_relations(self, relations: Sequence[Relation]) -> None:
        """Store relations between sources already stored, by citekey."""
        ...

    def status_counts(self) -> Dict[str, int]: ...
    def recorded_vault_file(self, path: str) -> Optional[int]:
        """The id of a vault file whose fixity was already recorded this build, else None."""
        ...

    def record_vault_file(self, path: str, fingerprint: Mapping[str, object], acquisition: Mapping[str, Optional[str]]) -> int:
        """The id of the vault file `path` (created on first sight), with its fingerprint and custodial history."""
        ...
