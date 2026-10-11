"""Composition root for entity command use cases."""
import entities_store
import entity_migration_store
import entity_queries_store
import entities_service
import private_git
from wiring import bind

DEPS = entities_service.EntitiesDeps(git=private_git, entity_files=entities_store, entity_migration=entity_migration_store,
                                      entity_queries=entity_queries_store)


read_file = bind(entities_service.load_entities, DEPS)
find = bind(entities_service.find, DEPS)
link_counts = bind(entities_service.link_counts, DEPS)
add = bind(entities_service.add, DEPS)
alias = bind(entities_service.alias, DEPS)


def tag(ref, allow_dirty=False, dry_run=False):
    return entities_service.set_private(DEPS, ref, True, allow_dirty, dry_run)


def untag(ref, allow_dirty=False, dry_run=False):
    return entities_service.set_private(DEPS, ref, False, allow_dirty, dry_run)


migrate_keywords = bind(entities_service.migrate_keywords, DEPS)
