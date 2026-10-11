"""Port for linking facts to entities in a built database: write the entities, read what facts say, write links."""
from dataclasses import dataclass
from typing import Any, List, Mapping, Protocol, Tuple


@dataclass(frozen=True)
class FactMention:
    """A fact as entity linking reads it: the free text to match and how it is stored."""
    fact_id: int
    texts: Tuple[Any, ...]
    visibility: str


class EntityLinkTable(Protocol):
    def add_entity(self, entity: Mapping, name_norm: str) -> int: ...
    def add_alias(self, entity_row: int, alias: str, alias_norm: str) -> None: ...
    def fact_mentions(self) -> List[FactMention]: ...
    def link(self, fact_id: int, entity_row: int) -> None: ...
