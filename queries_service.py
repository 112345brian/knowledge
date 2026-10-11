"""The read side as an application service: look facts, subjects, history, audits and build info up through the
`FactReads` port. Plain data in, plain data out; never prints or exits. `queries` is the composition root that
binds it to the real adapter, and `knowledge.py` is a thin printer over that."""
from dataclasses import dataclass

from fact_reads_port import FactReads


@dataclass(frozen=True)
class QueriesDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    reads: FactReads


def search_facts(deps, terms, **filters):
    return deps.reads.search_facts(terms, **filters)


def list_facts(deps, **filters):
    return deps.reads.list_facts(**filters)


def get_fact(deps, fact_id):
    return deps.reads.get_fact(fact_id)


def list_subjects(deps, include_pending=False):
    return deps.reads.list_subjects(include_pending=include_pending)


def fact_as_of(deps, fact_id, as_of):
    return deps.reads.fact_as_of(fact_id, as_of)


def history(deps, ref):
    """The revisions of a fact (id or source_key), oldest first; [] when there are none."""
    return deps.reads.get_history(ref)


def source_ids(deps, citekey):
    """A source's identifiers and the ones it shares with another source, or None for an unknown citekey."""
    return deps.reads.source_ids(citekey)


def pending(deps):
    """Pending (unreviewed) facts of the built db, oldest first."""
    return deps.reads.list_pending()


def audit_claims(deps):
    """(claims with a stale premise, cited facts whose recheck_by is not an ISO date)."""
    return deps.reads.audit_claims()


def audit_sources(deps):
    return deps.reads.audit_sources()


def audit_source_status(deps):
    return deps.reads.audit_source_status()


def build_info(deps, compare_db=None):
    """(this db's latest build info or None, the other db's when `compare_db` is given, the differing inputs
    or None when nothing was compared). Raises DatabaseNotFound for a missing db."""
    mine = deps.reads.latest_build_info()
    if compare_db is None:
        return mine, None, None
    other = deps.reads.latest_build_info(compare_db)
    return mine, other, deps.reads.compare_build_info(other, mine)


def subject_context(deps):
    """({subject: parent}, known subjects), or None when there is no usable db."""
    return deps.reads.subject_context()


def existing_db_path(deps):
    return deps.reads.existing_db_path()


def default_db_path(deps):
    return deps.reads.default_db_path()
