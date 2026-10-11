"""Port for running the standalone scripts as processes.

Port for starting this repo's standalone scripts and tools as separate processes. The adapter
(`script_runner`) owns the interpreter, the working directory and `subprocess`; callers get an exit code."""
from typing import Protocol, Sequence


class ScriptRunner(Protocol):
    def run_script(self, name: str, args: Sequence[str] = ()) -> int: ...
    def run_module(self, module: str, args: Sequence[str] = ()) -> int: ...
