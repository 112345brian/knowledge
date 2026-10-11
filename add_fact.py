"""Composition root for the single-fact use case: binds `add_fact_service` to the real adapters (the facts
file, the privacy rules, git, the clock, fresh ids) and keeps the public API (`append_fact`, `validate_fact`,
`record_fact`, ...). The `python3 add_fact_cli.py` command line lives in `add_fact_cli`; nothing here parses
arguments, prints or exits.

Doesn't touch knowledge.db itself: rerun `python3 build.py` afterward to pick the new fact up.
"""
import os

import add_fact_service
import add_fact_store
import clock
import ids
import privacy_store
import paths
from wiring import bind
import private_git

HERE = os.path.dirname(os.path.abspath(__file__))


DEPS = add_fact_service.AddFactDeps(facts_file=add_fact_store, rules=privacy_store, clock=clock, ids=ids,
                                    defaults=paths.Defaults(), git=private_git)


def data_file_path():
    """The facts file this composition root writes to."""
    return paths.settings().facts_path


def data_file_label():
    """The facts file as it is shown in messages: relative to this repo."""
    return os.path.relpath(paths.settings().facts_path, HERE)


def new_source_key():
    return ids.new_source_key()


validate_fact = bind(add_fact_service.validate_fact, DEPS)
build_entry = bind(add_fact_service.build_entry, DEPS)
resolve_privacy = bind(add_fact_service.resolve_privacy, DEPS)
canonicalize_subject = bind(add_fact_service.canonicalize_subject, DEPS)
append_fact = bind(add_fact_service.append_fact, DEPS)
record_fact = bind(add_fact_service.record_fact, DEPS)
