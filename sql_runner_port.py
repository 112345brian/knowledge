"""Port for running SQL against a database file by path: the leak self-test's fixture builder and its
marker-injection step. The caller supplies scripts and parameterised statements as data; nothing is returned."""
from typing import Any, Protocol, Sequence, Tuple


class SqlRunner(Protocol):
    def run(self, path, scripts: Sequence[str], statements: Sequence[Tuple[str, Tuple[Any, ...]]]) -> None:
        """Open (creating if needed) the SQLite file at `path` with foreign keys on, execute each script, then
        each (sql, params) statement in order, commit and close."""
        ...
