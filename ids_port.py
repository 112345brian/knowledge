"""Port for creating fresh source identifiers."""
from typing import Protocol


class Ids(Protocol):
    def new_source_key(self) -> str: ...
