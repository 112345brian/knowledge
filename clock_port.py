"""Port for the application's clock."""

__arbor__ = {"ring": "domain_services"}

from typing import Any, Protocol


class Clock(Protocol):
    is_fixed: bool

    def now(self) -> Any: ...
    def now_iso(self) -> str: ...
