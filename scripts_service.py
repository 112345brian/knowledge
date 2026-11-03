"""The use cases that are a script run: rebuild the database, or run a tool a client source ships.

Application ring (tach layer `application`): it orchestrates through the `ScriptRunner` port and never imports an
adapter. It decides which script, with which arguments and for which configuration; the port runs it and returns its
exit code. Never prints or exits. `scripts` is the composition root that binds it to the real runner.

A child process re-reads its configuration, so the service hands it exactly the container's: the config file the
`Locations` came from and, for a fixed clock, its instant. How the child is told is the runner's business. Locations that did not come
from a config file (built by hand) cannot start a script: that would silently run it against whatever the
environment happens to hold."""
from dataclasses import dataclass

from clock_port import Clock
from locations import Locations
from script_runner_port import ScriptRunner, ScriptSettings

@dataclass(frozen=True)
class ScriptsDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    runner: ScriptRunner
    clock: Clock
    locations: Locations


def child_settings(deps):
    """The configuration of a child started for these dependencies; ValueError when the locations have no config file."""
    if deps.locations.config_file is None:
        raise ValueError("these locations were not read from a config file (paths.load()), so a script cannot "
                         "be started for them: it would run against whatever the environment holds")
    return ScriptSettings(deps.locations.config_file, deps.clock.now_iso() if deps.clock.is_fixed else None)


def build(deps, check=False):
    """Rebuild knowledge.db (`check`: into a throwaway file, live db untouched). Returns build.py's exit code."""
    return deps.runner.run_script("build.py", ["--check"] if check else [], settings=child_settings(deps))


def run_tool(deps, module):
    """Run a client tool (`python -m <module>`, as the client's bootstrap registers it). Returns its exit code."""
    return deps.runner.run_module(module, settings=child_settings(deps))
