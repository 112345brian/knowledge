"""The commands that are a script run: rebuild the database, clean the concerts CSV. The service decides which
script, with which arguments and for which configuration; the `ScriptRunner` port runs it and returns its exit
code. Never prints or exits. `scripts` is the composition root that binds it to the real runner.

A child process re-reads its configuration from its environment, so the service hands it exactly the container's:
the private checkout the `Locations` came from and, for a fixed clock, its instant. Locations that did not come
from a checkout (built by hand) cannot start a script: that would silently run it against whatever the
environment happens to hold."""
from dataclasses import dataclass

from clock_port import Clock
from locations import Locations
from script_runner_port import ScriptRunner

PRIVATE_DIR_VAR = "KNOWLEDGE_PRIVATE_DIR"
FROZEN_NOW_VAR = "KNOWLEDGE_FROZEN_NOW"


@dataclass(frozen=True)
class ScriptsDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    runner: ScriptRunner
    clock: Clock
    locations: Locations


def child_env(deps):
    """The configuration of a child started for these dependencies; ValueError when the locations have no checkout."""
    if deps.locations.private_dir is None:
        raise ValueError("these locations were not read from a private checkout (paths.load()), so a script cannot "
                         "be started for them: it would run against whatever the environment holds")
    env = {PRIVATE_DIR_VAR: deps.locations.private_dir}
    if deps.clock.is_fixed:
        env[FROZEN_NOW_VAR] = deps.clock.now_iso()
    return env


def build(deps, check=False):
    """Rebuild knowledge.db (`check`: into a throwaway file, live db untouched). Returns build.py's exit code."""
    return deps.runner.run_script("build.py", ["--check"] if check else [], env=child_env(deps))


def clean_concerts(deps):
    """Clean the concerts export CSV in place (dedupes rows). Returns the tool's exit code."""
    return deps.runner.run_module("client.clean_concerts_csv", env=child_env(deps))
