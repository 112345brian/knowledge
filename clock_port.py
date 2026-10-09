"""Port for the application's clock."""
from typing import Any, Protocol


class Clock(Protocol):
    def now(self) -> Any: ...
    def now_iso(self) -> str: ...
