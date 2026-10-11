"""Ports of the fact ingest: the fact repository the two fact steps write through, and the privacy floor they apply
after the last insert.

Every method takes the build connection first, like `step_stores_port.StepStores`: the connection is the unit of
work the build owns, and an in-memory fake simply ignores it. The adapter is `fact_ingest_store`.

The identity operations (`subject_id`, `source_ids_by_citekey`, `fact_exists`) are the ones the foundation
contract's subject and source repositories also provide; they sit here until that contract lands, so the service
asks for exactly these and nothing else of them.
"""
from typing import Any, Dict, Optional, Protocol, Tuple


class FactRepository(Protocol):
    # --- identity (subjects, sources, facts)
    def subject_id(self, con, name, domain=None, resolve_aliases=False) -> int:
        """The id of the subject `name`, created if absent (with `domain` when given). With `resolve_aliases` a
        name that is an alias of a subject resolves to it (#43)."""
        ...
    def source_ids_by_citekey(self, con) -> Dict[str, int]: ...
    def fact_exists(self, con, source_key) -> bool: ...

    # --- vault files (an origin note and its fingerprint, #38)
    def find_vault_file(self, con, path) -> Optional[Tuple[int, bool]]:
        """(id, whether its fingerprint is recorded) of the row for `path`, or None."""
        ...
    def add_vault_file(self, con, path) -> int: ...
    def record_vault_file(self, con, file_id, fingerprint, acquisition) -> None: ...

    # --- the fact and what hangs off it
    def insert_fact(self, con, row) -> int:
        """Insert a `fact_ingest_decisions.FactRow`; the new fact's id."""
        ...
    def insert_revision(self, con, fact_id, revision) -> None: ...
    def link_source(self, con, fact_id, source_id, locator, quote) -> None: ...

    # --- after the last insert
    def facts_by(self, con, column) -> Dict[Any, int]: ...
    def commit(self, con) -> None: ...


class PrivacyFloor(Protocol):
    """The privacy rules as the build applies them: loaded first (a corrupt file fails early), applied after the
    last insert (raise-only; returns {"raised": [...], ...})."""
    def load_privacy_rules(self, path) -> Any: ...
    def apply_privacy_rules(self, con, rules) -> Any: ...
