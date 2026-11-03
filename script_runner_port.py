"""The port for starting this repo's standalone scripts and tools as separate processes.

Domain-services ring (tach layer `domain_services`): a pure contract with no I/O, defined by the inner ring that needs
it and implemented by the outer one. `scripts_service` (application) calls it; the adapter `script_runner`
(infrastructure) owns the interpreter, the working directory and `subprocess`; callers get an exit code.

A child process is configured only by the `ScriptSettings` the caller passes (which config file, which fixed
time). How a child is told is the adapter's business, not the caller's: the adapter drops whatever the parent's own
environment says about them first, so a child can never silently pick up another config file or clock than the one
it was started for. (Per-setting overrides such as KNOWLEDGE_DB_DIR are inherited on purpose: the container's
locations were read under them.)"""

__arbor__ = {"ring": "domain_services"}

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence


@dataclass(frozen=True)
class ScriptSettings:
    """What a started script is configured with: the config file to read and, for a fixed clock, its instant. Plain
    values in the form the caller already has them; the runner decides how a child is told."""
    config_file: str
    frozen_now: Optional[str] = None      # an ISO instant when the clock is fixed, else the child uses the real one


class ScriptRunner(Protocol):
    """Starts a script or a module as a separate process configured with `settings`, and returns its exit code."""
    def run_script(self, name: str, args: Sequence[str] = (), settings: Optional[ScriptSettings] = None) -> int:
        """Run the repo script `name` with `args`, configured with `settings`; return its exit code."""
        ...

    def run_module(self, module: str, args: Sequence[str] = (), settings: Optional[ScriptSettings] = None) -> int:
        """Run `python -m module` with `args`, configured with `settings`; return its exit code."""
        ...
