"""Adapter for `fact_reads_port.FactReads`: each read opens its own read-only connection to the built
knowledge.db, runs one `fact_queries` / `claims_store` function and closes it. Callers never see a connection.
A db built with an older schema, or bad full-text syntax, surfaces as `FactQueryError`."""
import functools
import sqlite3

import claims_store
import fact_queries
import review_store
from fact_reads_port import DatabaseNotFound, FactQueryError  # noqa: F401  (DatabaseNotFound is raised by connect)


def _one_shot(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        con = fact_queries.connect()
        try:
            return fn(con, *args, **kwargs)
        except sqlite3.OperationalError as e:
            raise FactQueryError(str(e)) from e
        finally:
            con.close()
    return run


search_facts = _one_shot(fact_queries.search_facts)
list_facts = _one_shot(fact_queries.list_facts)
get_fact = _one_shot(fact_queries.get_fact)
list_subjects = _one_shot(fact_queries.list_subjects)
fact_as_of = _one_shot(fact_queries.fact_as_of)
get_history = _one_shot(fact_queries.get_history)
source_ids = _one_shot(fact_queries.source_ids)
list_pending = _one_shot(review_store.list_pending)
audit_sources = _one_shot(fact_queries.audit_sources)
audit_source_status = _one_shot(fact_queries.audit_source_status)


@_one_shot
def audit_claims(con):
    return claims_store.audit_claims(con), claims_store.unparseable_rechecks(con)


def latest_build_info(db_path=None):
    """The latest build row of `db_path` (default: the configured db), or None."""
    con = fact_queries.connect(db_path)
    try:
        return fact_queries.latest_build_info(con)
    except sqlite3.OperationalError as e:
        raise FactQueryError(str(e)) from e
    finally:
        con.close()


compare_build_info = fact_queries.compare_build_info
subject_context = fact_queries.subject_context
existing_db_path = fact_queries.existing_db_path
default_db_path = fact_queries.default_db_path
