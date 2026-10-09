"""Read port for inputs used by lifecycle decisions."""
from typing import Any, Dict, Optional, Protocol


class LifecycleReads(Protocol):
    DB_ERRORS: tuple

    def floor_inputs(self, data_dir, db) -> Any: ...
    def subjects(self, data_dir) -> Dict[str, Optional[str]]: ...
