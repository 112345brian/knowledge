"""Composition root for the read side: binds `queries_service` to the real read adapter (`fact_reads_store`).
The CLI calls these; it never opens a connection."""
import fact_reads_store
import queries_service
from wiring import bind_all

API = ("search_facts", "list_facts", "get_fact", "list_subjects", "fact_as_of", "history", "pending", "source_ids",
       "audit_claims", "audit_sources", "audit_source_status", "build_info", "subject_context", "existing_db_path",
       "default_db_path")


def compose(locations, clock):
    """The read-side API wired to the real read adapter for these `locations`."""
    deps = queries_service.QueriesDeps(fact_reads=fact_reads_store, clock=clock, locations=locations)
    return bind_all(queries_service, deps, API)
