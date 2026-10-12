"""Application boundary for Git operations on the private data repository.

The concrete subprocess implementation lives in ``private_git.py``. Composition roots inject
that adapter as each service's `git` dependency; services and adapters depend on this protocol instead.
"""
from typing import Any, Optional, Protocol


class PrivateGitError(Exception):
    """A git precondition or commit failed. The message is safe to show the user as is."""


class PrivateGit(Protocol):
    PrivateGitError: Any

    def find_repo(self, directory) -> Optional[str]: ...
    def ensure_clean_tree(self, repo_dir) -> None: ...
    def commit_private_change(self, paths, message, repo_dir) -> str: ...
    def is_detached(self, repo_dir) -> bool: ...
    def describe_repo(self, directory) -> dict: ...
