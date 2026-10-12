"""Build pipeline rules, domain: no file, no db, no clock.

The order of the build steps and which of them a checkout runs, which backups to prune, what a backup is called and which tables the build
report counts, and how a failed build stage is named (`BuildError`). `build_service` runs the build through ports;
`build.py` is the driving adapter that prints its outcome and sets the exit code.
"""
import re

KEEP_BACKUPS = 5
BACKUP_PREFIX = "knowledge.db.bak-"
BACKUP_DUPLICATE = re.compile(r"^(?P<base>.*?)(?:-(?P<n>\d+))?$")
DB_SIDECARS = ("", "-journal", "-wal", "-shm")


class BuildError(Exception):
    """A build stage failed; `stage` names it: a step filename, or one of STAGES."""
    def __init__(self, stage, cause):
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause


class NormalBuildError(BuildError):
    """Generating knowledge-normal.db failed. `published` says whether knowledge.db had already been swapped in
    (it stays in place) or the failure was in a `--check` build into a throwaway directory."""
    def __init__(self, cause, published):
        super().__init__("normal", cause)
        self.published = published


# The stages that are not a step file: where in the build a BuildError can come from besides the steps.
STAGES = ("config", "open", "schema", "build_info", "finalize", "backup", "publish")

# Every core build step in run order. Paths are relative to the repo root. Client sources add their own steps
# after one of these (or after each other) through `client_registry`; nothing here names one.
CORE_PIPELINE = (
    "ingest/seed_sources.py",
    "ingest/literature_sources.py",
    "ingest/facts.py",
    "ingest/seed_subject_hierarchy.py",
    "ingest/seed_general_facts.py",
    "ingest/apply_fact_revisions.py",
    "ingest/link_entities.py",
    "ingest/link_source_relations.py",
)

# The tables the build report counts that the core schema creates, in report order; a client source adds its own.
CORE_REPORT_TABLES = ("sources", "authors", "source_authors", "publishers", "facts", "subjects", "vault_files",
                      "source_identifiers", "build_info", "build_inputs", "source_relations", "entities",
                      "entity_aliases", "fact_entities", "claims", "claim_facts", "fact_sources", "fact_revisions")


def backup_name(now):
    """The file name of a backup taken at the datetime `now`."""
    return f"{BACKUP_PREFIX}{now:%Y%m%dT%H%M%S}"


def backup_candidate(name, attempt):
    """The `attempt`th (1-based) name to try for a backup whose natural name is `name`: `name`, `name-2`, `name-3`...
    Two rebuilds in one second, or under a fixed clock, must not overwrite each other's backup."""
    return name if attempt == 1 else f"{name}-{attempt}"


def next_backup_attempt(name, existing):
    """The attempt number to start from for a backup named `name`, given the file names in the backup directory:
    1 when none is `name` or `name-N`, else one past the highest N. Starting at the first FREE name would reuse
    the name of a backup that was pruned (the oldest of its second), and the new backup would then sort as the
    oldest and be pruned straight away."""
    highest = 0
    for existing_name in existing:
        base, n = _backup_order(existing_name)
        if base == name:
            highest = max(highest, n)
    return highest + 1


def _backup_order(name):
    """Sort key of a backup name, oldest first: the timestamp, then the duplicate counter as a NUMBER (`-10` is
    newer than `-9`, which plain string order gets backwards); the un-suffixed name is the first of its second."""
    m = BACKUP_DUPLICATE.match(name)
    base, n = m.group("base"), m.group("n")
    if n is None or not base.startswith(BACKUP_PREFIX) or len(base) <= len(BACKUP_PREFIX):
        return (name, 1)
    return (base, int(n))


def backups_to_prune(names, keep=KEEP_BACKUPS):
    """Of the backup file names `names`, the ones to delete so only the `keep` most recent remain --
    a large optional input can make a routine backup much bigger, so leaving these unpruned turns every rebuild
    into unbounded disk growth."""
    backups = sorted((f for f in names if f.startswith(BACKUP_PREFIX)), key=_backup_order, reverse=True)
    return backups[keep:]


def remove_db_files(remove_if_exists, path) -> None:
    """`path` and its journal/wal/shm sidecars, whichever exist; `remove_if_exists` is the file port's."""
    for suffix in DB_SIDECARS:
        remove_if_exists(path + suffix)
