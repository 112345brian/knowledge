"""Port for editing the persisted subject hierarchy: the I/O only. What an edit does to the entries is in subject_rules.
Reading the file as values is `build_repositories_port.SubjectRepository.read_file`; this port is only what the subject
commands (subjects_service) use."""
from typing import Any, Protocol
from private_git_port import PrivateGit


class SubjectFiles(Protocol):
    def edit_file(self, edit, what, message, allow_dirty=False, dry_run=False, *, path, git: PrivateGit) -> Any: ...
