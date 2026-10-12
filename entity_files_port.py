"""Port for the persisted entity file: the I/O only. What an edit does to the entities is in entity_rules."""
from typing import Any, Optional, Protocol, Sequence
from entity_rules import Entity
from private_git_port import PrivateGit


class EntityFileReader(Protocol):
    """Reading entities.json as values: the file half of `build_repositories_port.EntityRepository`, which satisfies it."""
    def read_file(self, path: str) -> Optional[Sequence[Entity]]:
        """None when the file is absent; a bad file raises `entity_rules.EntitiesError`."""


class EntityFiles(Protocol):
    def save(self, entities, path) -> None: ...
    def edit_file(self, edit, what, message, allow_dirty=False, dry_run=False, *, path, git: PrivateGit) -> Any: ...
