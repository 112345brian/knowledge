"""Port for migrating privacy keywords into private entities."""
from typing import Any, Protocol
from private_git_port import PrivateGit


class EntityMigration(Protocol):
    def migrate_keywords(self, allow_dirty=False, dry_run=False, keep_keywords=False, *, git: PrivateGit, data_dir=None) -> Any: ...
