"""Composition root for subject command use cases."""
import entity_queries_store
import subjects_service
import subjects_store
import private_git
from ports import bind

DEPS = subjects_service.SubjectsDeps(git=private_git, subject_files=subjects_store, entity_queries=entity_queries_store)


known = bind(subjects_service.known_subjects, DEPS)
rows = bind(subjects_service.rows, DEPS)
detail = bind(subjects_service.detail, DEPS)
alias = bind(subjects_service.alias, DEPS)
describe = bind(subjects_service.describe, DEPS)
deprecate = bind(subjects_service.deprecate, DEPS)
