"""Ports of the MCP use case: how it records a fact and how it looks facts up, without a file, db or git in sight.
`add_fact.compose(...)` and `queries.compose(...)` satisfy them; the use case never learns which."""
from typing import List, Protocol


class FactCapture(Protocol):
    def record_fact(self, fact, data_path=None, db_path=None, allow_dirty=False): ...


class Session(Protocol):
    """Names this server run in the provenance of every fact it records."""
    @property
    def session_id(self) -> str: ...


class FactLookup(Protocol):
    def search_facts(self, terms, **filters) -> List[dict]: ...
    def list_subjects(self, include_pending=False) -> List[dict]: ...
