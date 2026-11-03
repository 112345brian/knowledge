"""Read port for entity links and subject hierarchy query results."""

__arbor__ = {"ring": "domain_services"}

from typing import Any, Protocol


class EntityQueries(Protocol):
    def entity_link_counts(self, db_path) -> dict: ...
    def known_subjects(self, db_path) -> dict: ...
    def subject_rows(self, db: Any) -> list: ...
