"""Composition root for the read side: binds `queries_service` to the real read adapter (`fact_reads_store`).
The CLI calls these; it never opens a connection."""
import fact_reads_store
import queries_service
from wiring import bind

DEPS = queries_service.QueriesDeps(reads=fact_reads_store)

search_facts = bind(queries_service.search_facts, DEPS)
list_facts = bind(queries_service.list_facts, DEPS)
get_fact = bind(queries_service.get_fact, DEPS)
list_subjects = bind(queries_service.list_subjects, DEPS)
fact_as_of = bind(queries_service.fact_as_of, DEPS)
history = bind(queries_service.history, DEPS)
source_ids = bind(queries_service.source_ids, DEPS)
pending = bind(queries_service.pending, DEPS)
audit_claims = bind(queries_service.audit_claims, DEPS)
audit_sources = bind(queries_service.audit_sources, DEPS)
audit_source_status = bind(queries_service.audit_source_status, DEPS)
build_info = bind(queries_service.build_info, DEPS)
subject_context = bind(queries_service.subject_context, DEPS)
existing_db_path = bind(queries_service.existing_db_path, DEPS)
default_db_path = bind(queries_service.default_db_path, DEPS)
