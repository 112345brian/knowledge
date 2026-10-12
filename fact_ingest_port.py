"""Ports of the fact ingest that are not the build's repositories: which subject and vault file a name or path
means, the revision a loaded fact starts with, and the privacy floor the fact steps apply after the last insert.

The fact repository is the foundation's `build_repositories_port.FactRepository` (with its `SourceRepository` and
`UnitOfWork`); `FactStore` below is only the part of it the ingest calls, so the foundation's fakes and the SQLite
adapter both satisfy it. `link_measurements` is deliberately not here: linking facts to measurements is client logic
(`client/measurements_store`) and stays where it is. The adapters are `fact_ingest_store`'s.

Subjects and vault files are not a repository here: a subject is found or created by `subject_hierarchy_service` and a
vault file recorded by `source_ingest_service` (through `identity_service`), so the build has one definition of each.
"""
from typing import Any, Optional, Protocol


class FactStore(Protocol):
    """`build_repositories_port.FactRepository` less `find_by_source_key` and `link_measurements`: what the ingest
    calls. `add` takes a `build_records.NewFact`, `link_source` a `FactSourceLink`, `tally` a `FactTally` column."""
    def source_key_exists(self, source_key) -> bool: ...
    def add(self, fact) -> int: ...
    def link_source(self, link) -> None: ...
    def tally(self, by) -> Any: ...


class FactRevisionWriter(Protocol):
    """The one revision write the ingest makes: revision 1 of a fact it just inserted. The same method as
    `build_repositories_port.RevisionRepository.insert`, declared narrow because the ingest calls nothing else."""
    def insert(self, fact_id, revision) -> None: ...


class Identity(Protocol):
    """Which subject a name means and which vault file a path is, as the build already decides them. The adapter is
    `fact_ingest.BuildIdentity`, which hands both to the one definition (`subject_hierarchy_service`,
    `source_ingest_service`); this port exists so the ingest does not import a sibling use case."""
    def subject_id(self, name, domain=None, resolve_aliases=False) -> int:
        """The id of the subject `name` is filed under (created, with `domain` when given, if there is none). With
        `resolve_aliases` a name that is an alias of a subject resolves to it (#43)."""
        ...
    def vault_file_id(self, path) -> Optional[int]:
        """The vault_files id for `path`, its fixity recorded on first sight; None for no path."""
        ...


class PrivacyFloor(Protocol):
    """The privacy rules as the build applies them: loaded first (a corrupt file fails early), applied after the
    last insert (raise-only; returns {"raised": [...], ...})."""
    def load_privacy_rules(self, path) -> Any: ...
    def apply_privacy_rules(self, con, rules) -> Any: ...
