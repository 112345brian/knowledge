"""Port for starting this repo's standalone scripts and tools as separate processes. The adapter
(`script_runner`) owns the interpreter, the working directory and `subprocess`; callers get an exit code.

A child process is configured only by what the caller passes in `env` (which private checkout, which fixed
time): the adapter drops the parent's own KNOWLEDGE_PRIVATE_DIR and KNOWLEDGE_FROZEN_NOW first, so a child can
never silently pick up configuration other than the one it was started for."""
from typing import Mapping, Optional, Protocol, Sequence


class ScriptRunner(Protocol):
    def run_script(self, name: str, args: Sequence[str] = (), env: Optional[Mapping[str, str]] = None) -> int: ...
    def run_module(self, module: str, args: Sequence[str] = (), env: Optional[Mapping[str, str]] = None) -> int: ...
