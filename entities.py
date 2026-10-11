"""Composition root for entity command use cases."""
import entities_service
import entities_store
import entity_migration_store
import entity_queries_store
import private_git
from wiring import bind_all

API = {"read_file": "load_entities", "find": "find", "link_counts": "link_counts", "add": "add", "alias": "alias",
       "tag": "tag", "untag": "untag", "migrate_keywords": "migrate_keywords"}


def compose(locations):
    """The entity API wired to the real adapters for these `locations`."""
    deps = entities_service.EntitiesDeps(git=private_git, entity_files=entities_store,
                                         entity_migration=entity_migration_store,
                                         entity_queries=entity_queries_store, locations=locations)
    return bind_all(entities_service, deps, API)
