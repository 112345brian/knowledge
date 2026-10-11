"""Ports for building the normal-tier database: what is read from the full DB, what is written to the normal
one, and what the leak check reads. Each method takes a path and returns plain rows; no connection, cursor or
transaction crosses a port. The decisions (which rows, which texts, what counts as a leak) are `normal_rules`
and `leak_rules`; `normal_db_service` runs them; `normal_db_store` and `leak_scan_store` are the SQLite adapters."""
from typing import Any, Iterator, Protocol, Sequence, Tuple

from leak_rules import PrivateMaterial
from normal_rules import FullDbRows, Integrity, TableRows


class NormalSource(Protocol):
    def read_rows(self, full_path) -> FullDbRows:
        """The full DB's rows `select_content` works from, read from one read-only snapshot."""
        ...


class NormalSink(Protocol):
    def write(self, path, content: Sequence[TableRows]) -> None:
        """Create the normal DB at `path` (it must not exist): its schema, the rows in the order given, and the
        full-text indexes rebuilt over them. Committed and closed when it returns."""
        ...

    def inspect(self, path) -> Integrity:
        """Foreign-key violations, row counts per whitelisted table, object names and trigger count of the DB."""
        ...


class LeakScan(Protocol):
    def cells(self, path) -> Iterator[Tuple[str, Any]]:
        """Every cell of every table of the database at `path` as (`table.column`, value)."""
        ...

    def private_material(self, full_path) -> PrivateMaterial:
        """The rows of the full DB at `full_path` that are private or must never be copied."""
        ...
