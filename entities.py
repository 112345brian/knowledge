"""Composition root for entity command use cases."""
import entities_store
import entity_migration_store
import entity_queries_store
import entities_service
import private_git
from ports import Ports, bind

ENTITY_TYPES = entities_store.ENTITY_TYPES
EntitiesError = entities_store.EntitiesError
PORTS = Ports(git=private_git, entity_files=entities_store, entity_migration=entity_migration_store,
              entity_queries=entity_queries_store)


read_file = bind(entities_service.load_entities, PORTS)
find = bind(entities_service.find, PORTS)
link_counts = bind(entities_service.link_counts, PORTS)
add = bind(entities_service.add, PORTS)
alias = bind(entities_service.alias, PORTS)


def tag(ref, allow_dirty=False, dry_run=False):
    return entities_service.set_private(PORTS, ref, True, allow_dirty, dry_run)


def untag(ref, allow_dirty=False, dry_run=False):
    return entities_service.set_private(PORTS, ref, False, allow_dirty, dry_run)


migrate_keywords = bind(entities_service.migrate_keywords, PORTS)
