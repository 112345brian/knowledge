"""Entity command use cases. All file and database work is supplied through ports."""
from dataclasses import dataclass
import entity_rules
from entity_files_port import EntityFileReader, EntityFiles
from entity_migration_port import EntityMigration
from entity_queries_port import EntityQueries
from private_git_port import PrivateGit
from locations import Locations


@dataclass(frozen=True)
class EntitiesDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    git: PrivateGit
    entity_files: EntityFiles
    entity_reader: EntityFileReader
    entity_migration: EntityMigration
    entity_queries: EntityQueries
    locations: Locations


def load_entities(deps: EntitiesDeps):
    assert deps.entity_reader is not None
    return [e.to_dict() for e in deps.entity_reader.read_file(deps.locations.entities_file()) or []]


def find(deps: EntitiesDeps, entities, ref):
    return entity_rules.find(entities, ref)


def link_counts(deps: EntitiesDeps):
    assert deps.entity_queries is not None
    return deps.entity_queries.entity_link_counts(deps.locations.db_path)


def add(deps: EntitiesDeps, name, entity_type, aliases=(), private=False, external_id=None, notes=None,
        allow_dirty=False, dry_run=False):
    assert deps.entity_files is not None
    assert deps.git is not None
    files = deps.entity_files
    return files.edit_file(
        lambda current: entity_rules.add_entity(current, name, entity_type, aliases, private, external_id, notes),
        f"add entity {name!r}" + (" (private)" if private else ""), f"entities: add {name}",
        allow_dirty, dry_run, path=deps.locations.entities_file(), git=deps.git)


def alias(deps: EntitiesDeps, ref, value, allow_dirty=False, dry_run=False):
    assert deps.entity_files is not None
    assert deps.git is not None
    files = deps.entity_files
    return files.edit_file(lambda current: entity_rules.add_alias(current, ref, value),
                           f"add alias {value!r} to {ref}", f"entities: alias {value} -> {ref}",
                           allow_dirty, dry_run, path=deps.locations.entities_file(), git=deps.git)


def set_private(deps: EntitiesDeps, ref, private, allow_dirty=False, dry_run=False):
    assert deps.entity_files is not None
    assert deps.git is not None
    files = deps.entity_files
    verb = "tag" if private else "untag"
    what = f"{verb} entity {ref}" + (" private" if private else "")
    return files.edit_file(lambda current: entity_rules.set_private(current, ref, private), what,
                           f"entities: {verb} {ref}" + (" private" if private else ""),
                           allow_dirty, dry_run, path=deps.locations.entities_file(), git=deps.git)


def migrate_keywords(deps: EntitiesDeps, allow_dirty=False, dry_run=False, keep_keywords=False):
    assert deps.entity_migration is not None
    assert deps.git is not None
    return deps.entity_migration.migrate_keywords(allow_dirty=allow_dirty, dry_run=dry_run,
                                                   keep_keywords=keep_keywords, git=deps.git,
                                                   data_dir=deps.locations.data_dir)


def tag(deps: EntitiesDeps, ref, allow_dirty=False, dry_run=False):
    """Make an entity private."""
    return set_private(deps, ref, True, allow_dirty, dry_run)


def untag(deps: EntitiesDeps, ref, allow_dirty=False, dry_run=False):
    """Make an entity not private."""
    return set_private(deps, ref, False, allow_dirty, dry_run)
