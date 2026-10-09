"""Port for configured fact-file and database locations."""
from typing import Protocol


class FactDefaults(Protocol):
    data_path: str
    db_path: str
