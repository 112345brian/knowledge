"""Ports of the fact ingest: the fact repository the two fact steps write through, and the privacy floor they apply
after the last insert.

Every method takes the build connection first, like `step_stores_port.StepStores`: the connection is the unit of
work the build owns, and an in-memory fake simply ignores it. The adapter is `fact_ingest_store`.

Subjects and vault files are not here: a subject is found or created by `subject_hierarchy_service` and a vault file
recorded by `source_ingest_service`, so the build has one definition of each.
"""
from typing import Any, Dict, Optional, Protocol, Tuple


class FactRepository(Protocol):
    # --- identity (sources by citekey, facts by source key)
    def source_ids_by_citekey(self, con) -> Dict[str, int]: ...
    def fact_exists(self, con, source_key) -> bool: ...

    # --- the fact and what hangs off it
    def insert_fact(self, con, row) -> int:
        """Insert a `fact_ingest_decisions.FactRow`; the new fact's id."""
        ...
    def insert_revision(self, con, fact_id, revision) -> None: ...
    def link_source(self, con, fact_id, source_id, locator, quote) -> None: ...

    # --- after the last insert
    def facts_by(self, con, column) -> Dict[Any, int]: ...
    def commit(self, con) -> None: ...


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
