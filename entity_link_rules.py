"""Which facts mention which entities, and the invariant that a private mention is stored private (pure).

Linking is deterministic whole-word matching over the same free text the privacy resolver scans (entity_rules,
textmatch), so a link and a privacy decision cannot disagree. The invariant: a fact that mentions a private
entity must already be stored `private` (the privacy pass guarantees it; this proves it per build).
"""
from typing import Iterable, List, Mapping, Sequence

import entity_rules
from fact_state import Visibility


class UnprotectedPrivateMention(ValueError):
    """Facts mention a private entity but are not stored private; the build is refused."""

    def __init__(self, fact_ids: Sequence[int]):
        self.fact_ids = list(fact_ids)
        super().__init__(f"facts {self.fact_ids[:10]} mention a private entity but are not stored private; "
                         "refusing to build (the privacy pass should have raised them)")


def private_entity_ids(entities: Iterable[Mapping]) -> set:
    return {e["id"] for e in entities if e["private"]}


def mentioned(texts: Iterable, entities: Sequence[Mapping]) -> List[str]:
    """Ids of the entities mentioned anywhere in `texts`, in entity order."""
    return list(entity_rules.match_all(texts, entities))


def is_unprotected(hit_ids: Iterable[str], private_ids: set, visibility: str) -> bool:
    """True when the fact mentions a private entity and is not stored private."""
    return bool(private_ids & set(hit_ids)) and visibility != Visibility.PRIVATE
