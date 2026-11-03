"""Domain service (tach layer `domain_services`): ports of the database export and import: the database's rows out, the export folder's files, the client tables in scope, the notes, and a new database in."""

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
    """The export folder. Names are '/'-separated paths inside it, like `core/facts.ndjson`."""
    def read(self, name: str) -> str:
        """The text of file `name`."""
    def write(self, name: str, text: str) -> None:
        """Write `text` as file `name`, creating its directory."""


class ExportScope(Protocol):
    """Which client tables exist, as the client's registration says (the base does not know them)."""
    def bulk_tables(self, existing: Collection[str]) -> List[str]:
        """The client-owned tables among `existing`, sorted."""
    def known_bulk_tables(self) -> Collection[str]:
        """Every table any registered client source owns."""
    def schema_files(self, tables: Collection[str]) -> List[str]:
        """The schema files (schema.sql first) a database holding these client tables is created from."""


class NotesFiles(Protocol):
    """A directory of citation-note markdown files, flat: file names only."""
    def names(self) -> List[str]:
        """The file names of the notes, sorted."""
    def read(self, name: str) -> str:
        """The text of note `name`."""
    def write(self, name: str, text: str) -> None:
        """Write note `name`; FileExistsError if it exists."""


class ImportTarget(Protocol):
    """The new database an import fills."""
    def create(self, schema_files: Sequence[str]) -> None:
        """Create the new database from these schema files; FileExistsError if there is one."""
    def user_version(self) -> int:
        """The schema version (PRAGMA user_version) of the database."""
    def insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Insert `rows` into `table`."""
    def finish(self) -> List[str]:
        """Commit and return the problems found (empty: sound)."""
    def discard(self) -> None:
        """Remove the database `create` made."""
