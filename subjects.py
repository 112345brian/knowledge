"""Composition root for subject command use cases."""
import entity_queries_store
import subjects_service
import subjects_store
import private_git
from ports import Ports, bind

PORTS = Ports(git=private_git, subject_files=subjects_store, entity_queries=entity_queries_store)


known = bind(subjects_service.known_subjects, PORTS)
rows = bind(subjects_service.rows, PORTS)
detail = bind(subjects_service.detail, PORTS)
alias = bind(subjects_service.alias, PORTS)
describe = bind(subjects_service.describe, PORTS)
deprecate = bind(subjects_service.deprecate, PORTS)
