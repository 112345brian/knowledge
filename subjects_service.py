"""Subject command use cases. File and database work is supplied through ports."""
from ports import Ports


def known_subjects(ports: Ports):
    assert ports.entity_queries is not None
    return ports.entity_queries.known_subjects()


def rows(ports: Ports, db=None):
    assert ports.entity_queries is not None
    return ports.entity_queries.subject_rows(db)


def detail(ports: Ports, name):
    assert ports.entity_queries is not None
    rows = ports.entity_queries.subject_rows()
    by_name = {row["name"]: row for row in rows}
    via_alias = False
    if name not in by_name:
        owner = next((row for row in rows if name in row["aliases"]), None)
        if owner is None:
            return None
        name, via_alias = owner["name"], True
    result = dict(by_name[name], children=[row["name"] for row in rows if row["parent"] == name])
    result["resolved_from_alias"] = via_alias
    return result


def alias(ports: Ports, name, value, known, allow_dirty=False, dry_run=False):
    assert ports.subject_files is not None
    assert ports.git is not None
    files = ports.subject_files
    return files.edit_file(
        lambda entries: files.add_alias(entries, name, value, known=set(known), domain=known.get(name)),
        f"add alias {value} to {name}", f"subjects: alias {value} -> {name}", allow_dirty, dry_run,
        git=ports.git)


def describe(ports: Ports, name, text, known, allow_dirty=False, dry_run=False):
    assert ports.subject_files is not None
    assert ports.git is not None
    files = ports.subject_files
    return files.edit_file(
        lambda entries: files.describe(entries, name, text, known=set(known), domain=known.get(name)),
        f"set description of {name}", f"subjects: describe {name}", allow_dirty, dry_run, git=ports.git)


def deprecate(ports: Ports, name, replaced_by, known, allow_dirty=False, dry_run=False):
    assert ports.subject_files is not None
    assert ports.git is not None
    files = ports.subject_files
    return files.edit_file(
        lambda entries: files.deprecate(entries, name, replaced_by, known=set(known), domain=known.get(name)),
        f"deprecate {name}" + (f" (use {replaced_by})" if replaced_by else ""),
        f"subjects: deprecate {name}", allow_dirty, dry_run, git=ports.git)
