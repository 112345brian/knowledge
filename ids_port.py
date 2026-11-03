"""Port for creating fresh source identifiers."""

__arbor__ = {"ring": "domain_services"}

from typing import Protocol


class Ids(Protocol):
    def new_source_key(self) -> str: ...
