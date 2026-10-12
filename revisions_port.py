"""Port for persisted entries and the append-only revision log's writer.

Reading the log into the build database is `build_repositories_port.RevisionRepository` (`read_log`, `insert`,
`write_back`, ...); this port is only the editing side the CLI services use: the entry files and appending a
revision.
"""
from typing import Any, List, Protocol


class Revisions(Protocol):
    def load_entries(self, data_dir) -> List[dict]: ...
    def append_revision(self, source_key, changes, reason, via, session_id=None, data_dir=None,
                        revisions_path=None, expect=None, *, clock) -> Any: ...
