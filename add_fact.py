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
import new_fact  # noqa: F401  (add_fact.new_fact)
import privacy  # noqa: F401  (kept importable as add_fact.privacy)
import privacy_store
from add_fact_store import append_record, append_records  # noqa: F401  (re-exported: facts_batch, tests)
from fact_rules import FRESHNESS_VALUES, KIND_VALUES, SOURCE_KEY_RE, VALID_TRUST, VALID_VISIBILITY, VIA_RE  # noqa: F401  (re-exported: callers use add_fact.VALID_TRUST etc.)
from new_fact import (AddResult, DATE_RE, DataFileError, NewFact, SUBJECT_RE, VALID_NEW_STATUS)  # noqa: F401  (re-exported)
from paths import KNOWLEDGE_DB_DIR, PRIVATE_DATA_DIR
from ports import bind
import private_git

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(PRIVATE_DATA_DIR, "general_facts.json")
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")


class _Defaults:
    """Where the facts file and the db are, read from this module on every call (tests repoint them)."""
    data_path = property(lambda self: DATA_PATH)
    db_path = property(lambda self: DB_PATH)


DEPS = add_fact_service.AddFactDeps(facts_file=add_fact_store, rules=privacy_store, clock=clock, ids=ids,
                                    defaults=_Defaults(), git=private_git)


def data_file_label():
    """The facts file as it is shown in messages: relative to this repo."""
    return os.path.relpath(DATA_PATH, HERE)


def new_source_key():
    return ids.new_source_key()


validate_fact = bind(add_fact_service.validate_fact, DEPS)
build_entry = bind(add_fact_service.build_entry, DEPS)
resolve_privacy = bind(add_fact_service.resolve_privacy, DEPS)
canonicalize_subject = bind(add_fact_service.canonicalize_subject, DEPS)
append_fact = bind(add_fact_service.append_fact, DEPS)
record_fact = bind(add_fact_service.record_fact, DEPS)
