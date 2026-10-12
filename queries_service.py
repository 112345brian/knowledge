"""The read side as an application service: look facts, subjects, history, audits and build info up through the
`FactReads` port. Plain data in, plain data out; never prints or exits. `queries` is the composition root that
binds it to the real adapter, and `knowledge.py` is a thin printer over that."""
from dataclasses import dataclass

from clock_port import Clock
from fact_reads_port import FactReads
from locations import Locations


@dataclass(frozen=True)
class QueriesDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    fact_reads: FactReads
    clock: Clock
    locations: Locations


def search_facts(deps, terms, **filters):
    return deps.fact_reads.search_facts(deps.locations.db_path, terms, **filters)


def list_facts(deps, **filters):
    return deps.fact_reads.list_facts(deps.locations.db_path, **filters)


def get_fact(deps, fact_id):
    return deps.fact_reads.get_fact(deps.locations.db_path, fact_id)


def list_subjects(deps, include_pending=False):
    return deps.fact_reads.list_subjects(deps.locations.db_path, include_pending=include_pending)


def fact_as_of(deps, fact_id, as_of):
    return deps.fact_reads.fact_as_of(deps.locations.db_path, fact_id, as_of)


def history(deps, ref):
    """The revisions of a fact (id or source_key), oldest first; [] when there are none."""
    return deps.fact_reads.get_history(deps.locations.db_path, ref)


def pending(deps):
    """Pending (unreviewed) facts of the built db, oldest first."""
    return deps.fact_reads.list_pending(deps.locations.db_path)


def source_ids(deps, citekey):
    """A source's identifiers and the ones it shares with another source, or None for an unknown citekey."""
    return deps.fact_reads.source_ids(deps.locations.db_path, citekey)


def audit_claims(deps):
    """(claims with a stale premise, cited facts whose recheck_by is not an ISO date)."""
    return deps.fact_reads.audit_claims(deps.locations.db_path, deps.clock.now().date())


def audit_sources(deps):
    return deps.fact_reads.audit_sources(deps.locations.db_path, deps.locations.notes_vault)


def audit_source_status(deps):
    return deps.fact_reads.audit_source_status(deps.locations.db_path)


def build_info(deps, compare_db=None):
    """(this db's latest build info or None, the other db's when `compare_db` is given, the differing inputs
    or None when nothing was compared). Raises DatabaseNotFound for a missing db."""
    mine = deps.fact_reads.latest_build_info(deps.locations.db_path)
    if compare_db is None:
        return mine, None, None
    other = deps.fact_reads.latest_build_info(compare_db)
    return mine, other, deps.fact_reads.compare_build_info(other, mine)


def subject_context(deps):
    """({subject: parent}, known subjects), or None when there is no usable db."""
    return deps.fact_reads.subject_context(deps.locations.db_path, deps.locations.data_path)


def existing_db_path(deps):
    """The built db's path when it exists, else None (a source_key still resolves without one)."""
    return deps.fact_reads.existing_db_path(deps.locations.db_path)


def default_db_path(deps):
    """Where the built db is (whether or not it exists yet)."""
    return deps.locations.db_path
