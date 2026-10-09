"""Entity command use cases. All file and database work is supplied through ports."""
from ports import Ports


def load_entities(ports: Ports):
    assert ports.entity_files is not None
    return ports.entity_files.read_file() or []


def find(ports: Ports, entities, ref):
    assert ports.entity_files is not None
    return ports.entity_files.find(entities, ref)


def link_counts(ports: Ports):
    assert ports.entity_queries is not None
    return ports.entity_queries.entity_link_counts()


def add(ports: Ports, name, entity_type, aliases=(), private=False, external_id=None, notes=None,
        allow_dirty=False, dry_run=False):
    assert ports.entity_files is not None
    assert ports.git is not None
    files = ports.entity_files
    return files.edit_file(
        lambda current: files.add_entity(current, name, entity_type, aliases, private, external_id, notes),
        f"add entity {name!r}" + (" (private)" if private else ""), f"entities: add {name}",
        allow_dirty, dry_run, git=ports.git)


def alias(ports: Ports, ref, value, allow_dirty=False, dry_run=False):
    assert ports.entity_files is not None
    assert ports.git is not None
    files = ports.entity_files
    return files.edit_file(lambda current: files.add_alias(current, ref, value),
                           f"add alias {value!r} to {ref}", f"entities: alias {value} -> {ref}",
                           allow_dirty, dry_run, git=ports.git)


def set_private(ports: Ports, ref, private, allow_dirty=False, dry_run=False):
    assert ports.entity_files is not None
    assert ports.git is not None
    files = ports.entity_files
    verb = "tag" if private else "untag"
    what = f"{verb} entity {ref}" + (" private" if private else "")
    return files.edit_file(lambda current: files.set_private(current, ref, private), what,
                           f"entities: {verb} {ref}" + (" private" if private else ""),
                           allow_dirty, dry_run, git=ports.git)


def migrate_keywords(ports: Ports, allow_dirty=False, dry_run=False, keep_keywords=False):
    assert ports.entity_migration is not None
    assert ports.git is not None
    return ports.entity_migration.migrate_keywords(allow_dirty=allow_dirty, dry_run=dry_run,
                                                   keep_keywords=keep_keywords, git=ports.git)
