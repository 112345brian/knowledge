"""The commands that are a script run: rebuild the database, clean the concerts CSV. The service decides which
script and with which arguments; the `ScriptRunner` port runs it and returns its exit code. Never prints or
exits. `scripts` is the composition root that binds it to the real runner."""
from dataclasses import dataclass

from script_runner_port import ScriptRunner


@dataclass(frozen=True)
class ScriptsDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    runner: ScriptRunner


def build(deps, check=False):
    """Rebuild knowledge.db (`check`: into a throwaway file, live db untouched). Returns build.py's exit code."""
    return deps.runner.run_script("build.py", ["--check"] if check else [])


def clean_concerts(deps):
    """Clean the concerts export CSV in place (dedupes rows). Returns the tool's exit code."""
    return deps.runner.run_module("client.clean_concerts_csv")
