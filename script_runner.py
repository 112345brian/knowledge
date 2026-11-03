"""Starts the standalone scripts (build.py and the client tools) as child processes: the adapter behind `script_runner_port`.

Infrastructure ring (tach layer `infrastructure`): it implements the `ScriptRunner` port with `subprocess`, and is the
only place the CLI starts a Python subprocess, so the Typer app itself imports no `subprocess`. It alone knows that a
child's `ScriptSettings` travel as the KNOWLEDGE_CONFIG and KNOWLEDGE_FROZEN_NOW environment variables."""
import os
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
# The two variables that choose a child's config file and clock; this adapter owns how a child is told. The
# first is the one paths.py reads (tests/test_paths.py pins that the two spellings agree). The parent's own values
# are never inherited.
CONFIG_ENV_VAR = "KNOWLEDGE_CONFIG"
FROZEN_NOW_ENV_VAR = "KNOWLEDGE_FROZEN_NOW"


def child_environment(settings=None, base=None):
    """The environment a child is started with: `base` (default: this process's) without the two configuration
    variables, plus those `settings` (a `ScriptSettings`) asks for."""
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k not in (CONFIG_ENV_VAR, FROZEN_NOW_ENV_VAR)}
    if settings is not None:
        env[CONFIG_ENV_VAR] = settings.config_file
        if settings.frozen_now is not None:
            env[FROZEN_NOW_ENV_VAR] = settings.frozen_now
    return env


def run_script(name, args=(), settings=None):
    """Run `name` (a script next to this file) with `args`, configured with `settings`, and return its exit code."""
    return subprocess.call([sys.executable, os.path.join(HERE, name), *args], env=child_environment(settings))


def run_module(module, args=(), settings=None):
    """Run `python -m module` from the repo root (for the tools in the `ingest` package), configured with `settings`, and
    return its exit code."""
    return subprocess.call([sys.executable, "-m", module, *args], cwd=HERE, env=child_environment(settings))
