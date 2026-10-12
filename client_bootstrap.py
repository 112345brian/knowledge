"""Composition: the one place the base looks for the client's registration, and the only place that does.

`registry()` returns a `ClientRegistry` over the core pipeline. When the checkout has a `client/bootstrap.py`
(the client directory is recognised), that module is loaded by path and its `register(registry)` is called; with no
client directory the registry is empty and the build is core only. A client's own sources are registered there, in
its directory, so nothing in the base changes when one is added.
"""
import os

import build_rules
import local_buildfiles
from client_registry import ClientRegistry

BOOTSTRAP = "client/bootstrap.py"


def code_dir():
    return os.path.dirname(os.path.abspath(__file__))


def registry(files=None, code_dir_path=None) -> ClientRegistry:
    files = files or local_buildfiles
    here = code_dir_path or code_dir()
    found = ClientRegistry(build_rules.CORE_PIPELINE)
    path = files.join(here, BOOTSTRAP)
    if files.exists(path):
        module = files.load_module(path)
        register = getattr(module, "register", None)
        if register is None:
            raise AttributeError(f"{BOOTSTRAP} defines no register(registry)")
        register(found)
    return found
