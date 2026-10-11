"""Ports for applying the privacy rules to a built database: the rules, and the subject / fact rows they touch."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple


@dataclass(frozen=True)
class FactText:
    """A fact as the privacy resolver reads it: its subject, statement, stored visibility and every other
    stored free-text field (notes, rationales, quote, applies_to, the citations' locators and quotes)."""
    fact_id: int
    subject: str
    statement: str
    visibility: str
    extra_text: Tuple[Optional[str], ...] = ()


class PrivacyRulesReader(Protocol):
    def load_rules(self, path) -> Any: ...


class FactPrivacyTable(Protocol):
    def subject_parents(self) -> Dict[str, Optional[str]]: ...
    def mark_subject(self, name, private: bool) -> None: ...
    def facts_to_scan(self) -> List[FactText]: ...
    def raise_to_private(self, fact_id) -> None: ...
