"""Composition root for subject command use cases."""
import entity_queries_store
import private_git
import subjects_service
import subjects_store
from wiring import bind_all

API = {"known": "known_subjects", "rows": "rows", "detail": "detail", "alias": "alias", "describe": "describe",
       "deprecate": "deprecate"}


def compose(locations):
    """The subject API wired to the real adapters for these `locations`."""
    deps = subjects_service.SubjectsDeps(git=private_git, subject_files=subjects_store,
                                         entity_queries=entity_queries_store, locations=locations)
    return bind_all(subjects_service, deps, API)
