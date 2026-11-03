"""Composition root for the single-fact use case: binds `add_fact_service` to the real adapters (the facts
file, the privacy rules, git, the clock, fresh ids) and keeps the public API (`append_fact`, `validate_fact`,
`record_fact`, ...). The `python3 add_fact_cli.py` command line lives in `add_fact_cli`; nothing here parses
arguments, prints or exits.

Doesn't touch knowledge.db itself: rerun `python3 build.py` afterward to pick the new fact up.
"""

__arbor__ = {"ring": "composition"}

import os

import add_fact_service
import add_fact_store
import ids
import private_git
import privacy_store
from wiring import bind_all

HERE = os.path.dirname(os.path.abspath(__file__))
API = ("validate_fact", "build_entry", "resolve_privacy", "canonicalize_subject", "append_fact", "record_fact")


def data_file_label(data_path, here=HERE):
    """The facts file as it is shown in messages: relative to `here` (this repo), or its absolute path when there is
    no relative form. On Windows a file on another drive than the repo has none, and `os.path.relpath` raises for it;
    a label for a message must never fail the command that already wrote the fact."""
    try:
        return os.path.relpath(data_path, here)
    except ValueError:
        return data_path


def compose(locations, clock):
    """The single-fact API wired to the real adapters for these `locations`. `data_file_label()` is the facts
    file as it is shown in messages: relative to this repo."""
    deps = add_fact_service.AddFactDeps(facts_file=add_fact_store, rules=privacy_store, clock=clock, ids=ids,
                                        git=private_git, locations=locations)
    api = bind_all(add_fact_service, deps, API)
    api.data_file_path = lambda: locations.data_path
    api.data_file_label = lambda: data_file_label(locations.data_path)
    return api
