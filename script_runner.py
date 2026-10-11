"""Adapter for the standalone scripts (build.py, clean_concerts_csv.py): `knowledge.py build`
and `clean-concerts` are thin dispatches to them. This is the only place the CLI starts a Python
subprocess, so the Typer app itself imports no `subprocess`."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# The two variables that configure a child process. The parent's own values are never inherited.
CONFIG_VARS = ("KNOWLEDGE_PRIVATE_DIR", "KNOWLEDGE_FROZEN_NOW")


def child_environment(overrides=None, base=None):
    """The environment a child is started with: `base` (default: this process's) without the two configuration
    variables, plus `overrides`."""
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k not in CONFIG_VARS}
    env.update(overrides or {})
    return env


def run_script(name, args=(), env=None):
    """Run `name` (a script next to this file) with `args` and return its exit code."""
    return subprocess.call([sys.executable, os.path.join(HERE, name), *args], env=child_environment(env))


def run_module(module, args=(), env=None):
    """Run `python -m module` from the repo root (for the tools in the `ingest` package)."""
    return subprocess.call([sys.executable, "-m", module, *args], cwd=HERE, env=child_environment(env))
