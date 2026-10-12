"""Port for sources: where normalized source records come from.

`SourceReader` yields the records of one origin (manual_sources.json, the vault's citation notes, or several composed).
Where sources are kept is `build_repositories_port.SourceRepository`; its SQLite adapter is `source_repository_store`.
"""
from typing import List, Protocol

from source_record import SourceRecord
from source_status import Relation


class SourceReader(Protocol):
    def records(self) -> List[SourceRecord]:
        """Every source of this origin, validated. Reads the origin afresh on each call."""
        ...

    def relations(self) -> List[Relation]:
        """Only the relations the origin declares, read afresh (a reader that has no cheaper way re-reads and
        validates everything)."""
        ...
