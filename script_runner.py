"""Adapter for the standalone scripts (build.py, add_fact.py, clean_concerts_csv.py): `knowledge.py build`,
`add-fact` and `clean-concerts` are thin dispatches to them. This is the only place the CLI starts a Python
subprocess, so the Typer app itself imports no `subprocess`."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run_script(name, args=()):
    """Run `name` (a script next to this file) with `args` and return its exit code."""
    return subprocess.call([sys.executable, os.path.join(HERE, name), *args])
