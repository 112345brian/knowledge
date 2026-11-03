"""Domain service (tach layer `domain_services`): ports of the database export and import: the database's rows out, the export directory's files, and a new database in."""

__arbor__ = {"ring": "domain_services"}

from typing import Any, Collection, Iterator, List, Mapping, Protocol, Sequence


class ExportReads(Protocol):
    """What an export reads from the database."""
    def user_version(self) -> int:
        """The schema version (PRAGMA user_version) of the database."""
    def tables(self) -> Collection[str]:
        """The names of the tables that exist."""
    def rows(self, table: str) -> Iterator[Mapping[str, Any]]:
        """Every row of `table` as a column-name mapping, in primary-key order."""


class ExportFiles(Protocol):
    """The directory of export files."""
    def read(self, name: str) -> str:
        """The text of file `name`."""
    def write(self, name: str, text: str) -> None:
        """Write `text` as file `name`."""


class ImportTarget(Protocol):
    """The new database an import fills."""
    def create(self) -> None:
        """Create the new database with the core schema."""
    def user_version(self) -> int:
        """The schema version (PRAGMA user_version) of the database."""
    def insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Insert `rows` into `table`."""
    def finish(self) -> List[str]:
        """Commit and return the problems found (empty: sound)."""
    def discard(self) -> None:
        """Remove the database `create` made."""
