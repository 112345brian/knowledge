"""Subject command use cases. File and database work is supplied through ports."""
from dataclasses import dataclass
from entity_queries_port import EntityQueries
from private_git_port import PrivateGit
from subject_files_port import SubjectFiles
from locations import Locations


@dataclass(frozen=True)
class SubjectsDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    git: PrivateGit
    subject_files: SubjectFiles
    entity_queries: EntityQueries
    locations: Locations


def known_subjects(deps: SubjectsDeps):
    assert deps.entity_queries is not None
    return deps.entity_queries.known_subjects(deps.locations.db_path)


def rows(deps: SubjectsDeps, db=None):
    assert deps.entity_queries is not None
    return deps.entity_queries.subject_rows(deps.locations.db_path if db is None else db)


def detail(deps: SubjectsDeps, name):
    assert deps.entity_queries is not None
    rows = deps.entity_queries.subject_rows(deps.locations.db_path)
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


def alias(deps: SubjectsDeps, name, value, known, allow_dirty=False, dry_run=False):
    assert deps.subject_files is not None
    assert deps.git is not None
    files = deps.subject_files
    return files.edit_file(
        lambda entries: files.add_alias(entries, name, value, known=set(known), domain=known.get(name)),
        f"add alias {value} to {name}", f"subjects: alias {value} -> {name}", allow_dirty, dry_run,
        path=deps.locations.subjects_file(), git=deps.git)


def describe(deps: SubjectsDeps, name, text, known, allow_dirty=False, dry_run=False):
    assert deps.subject_files is not None
    assert deps.git is not None
    files = deps.subject_files
    return files.edit_file(
        lambda entries: files.describe(entries, name, text, known=set(known), domain=known.get(name)),
        f"set description of {name}", f"subjects: describe {name}", allow_dirty, dry_run, path=deps.locations.subjects_file(), git=deps.git)


def deprecate(deps: SubjectsDeps, name, replaced_by, known, allow_dirty=False, dry_run=False):
    assert deps.subject_files is not None
    assert deps.git is not None
    files = deps.subject_files
    return files.edit_file(
        lambda entries: files.deprecate(entries, name, replaced_by, known=set(known), domain=known.get(name)),
        f"deprecate {name}" + (f" (use {replaced_by})" if replaced_by else ""),
        f"subjects: deprecate {name}", allow_dirty, dry_run, path=deps.locations.subjects_file(), git=deps.git)
