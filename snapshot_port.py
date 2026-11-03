"""Domain service (tach layer `domain_services`): ports of snapshots and of the in-place restore of chosen tables."""

__arbor__ = {"ring": "domain_services"}

from typing import Any, List, Mapping, Protocol, Sequence, Tuple


class Snapshots(Protocol):
    """The snapshot directory of one live database."""
    def create(self, name: str) -> str:
        """Take a verified, consistent copy of the live database as `name` in the snapshot directory, never over an
        existing file (FileExistsError); returns its path."""
    def names(self) -> List[str]:
        """Every file name in the snapshot directory (the caller filters to snapshots)."""
    def remove(self, name: str) -> None:
        """Delete snapshot `name`."""
    def check(self, path: str) -> Tuple[int, List[str]]:
        """(schema version, integrity problems) of the database file at `path`."""
    def copy_new(self, source: str, dest: str) -> None:
        """Copy the verified database `source` to the new file `dest`; FileExistsError if `dest` exists."""


class StagedRestore(Protocol):
    """A replacement that has been applied inside an open transaction and can still be undone."""
    def new_violations(self) -> List[str]:
        """The foreign-key problems the replacement introduced (those the database already had are not listed)."""
    def apply(self) -> None:
        """Make the replacement permanent."""
    def discard(self) -> None:
        """Undo the replacement, leaving the database as it was (a second call is harmless)."""


class TableRestore(Protocol):
    """Replace the rows of chosen tables inside an existing database, all or nothing."""
    def user_version(self) -> int:
        """The schema version of the database."""
    def tables(self) -> List[str]:
        """The tables that exist."""
    def stage(self, via: str, replacements: Mapping[str, Sequence[Mapping[str, Any]]]) -> StagedRestore:
        """In one write transaction (no other writer can start), delete every row of each named table and insert its
        replacement rows; `via` names the restore for the audit log when the database has one. Nothing is permanent
        until `apply`; a failure here leaves the database as it was."""
