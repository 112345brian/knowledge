"""Build pipeline rules, domain: no file, no db, no clock.

The order of the build steps and which of them a checkout runs, which backups to prune, what a backup is called and which tables the build
report counts, and how a failed build stage is named (`BuildError`). `build_service` runs the build through ports;
`build.py` is the driving adapter that prints its outcome and sets the exit code.
"""
import re

KEEP_BACKUPS = 5
BACKUP_PREFIX = "knowledge.db.bak-"
BACKUP_DUPLICATE = re.compile(r"^(?P<base>.*?)(?:-(?P<n>\d+))?$")


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

# The optional client sources: data a given client may or may not have and want in knowledge.db. A checkout
# enables the ones it uses with CLIENT_SOURCES in local_paths.py (see paths.py); the core pipeline needs none.
#   concerts      a concerts CSV                                   (CONCERTS_CSV)
#   ratings       a RateYourMusic ratings export                   (RYM_EXPORT_CSV)
#   scrobbles     a Last.fm scrobbles export                       (SCROBBLES_JSON)
#   measurements  structured measurements from an optional local source
#   claims        hand-authored claims from the private data directory
# concerts, ratings and scrobbles share the artists tables, so the first one enabled brings client/music.sql.
CLIENT_SOURCES = ("concerts", "ratings", "scrobbles", "measurements", "claims")
MUSIC_SOURCES = ("concerts", "ratings", "scrobbles")

# (schema file, the sources that need it): applied after schema.sql when any of its sources is enabled.
SCHEMA_FRAGMENTS = (
    ("client/music.sql", MUSIC_SOURCES),
    ("client/measurements.sql", ("measurements",)),
)

# The local_paths.py name of the input file each source reads; a source whose path is not set cannot be built.
REQUIRED_PATHS = {"concerts": "CONCERTS_CSV", "ratings": "RYM_EXPORT_CSV", "scrobbles": "SCROBBLES_JSON"}

# Every build step in run order, with the client source(s) it belongs to: None = core, a name, or a tuple meaning
# "any of these". Paths are relative to the repo root. A step runs when its source is None or enabled; the order
# is the same either way.
PIPELINE = (
    ("ingest/seed_sources.py", None),
    ("ingest/literature_sources.py", None),
    ("client/measurements.py", "measurements"),
    ("ingest/facts.py", None),
    ("client/link_fact_measurements.py", "measurements"),
    ("client/seed_claims.py", "claims"),
    ("ingest/seed_subject_hierarchy.py", None),
    ("client/concerts.py", "concerts"),
    ("client/music_ratings.py", "ratings"),
    ("client/scrobbles.py", "scrobbles"),
    ("client/seed_artist_members.py", MUSIC_SOURCES),
    ("ingest/seed_general_facts.py", None),
    ("ingest/apply_fact_revisions.py", None),
    ("ingest/link_entities.py", None),
    ("ingest/link_source_relations.py", None),
)

REPORT_TABLES = ("sources", "authors", "source_authors", "publishers", "metrics", "measurements", "facts", "subjects",
                 "vault_files", "source_identifiers", "build_info", "build_inputs", "source_relations", "entities", "entity_aliases", "fact_entities", "claims", "claim_facts", "fact_sources", "fact_revisions", "fact_measurements",
                 "exercises", "training_sets", "foods", "food_log_entries", "meal_log_entries",
                 "muscles", "muscle_volume_weekly",
                 "artists", "artist_members", "venues", "festivals", "concert_attendances", "albums",
                 "tracks", "import_sources", "scrobbles")


def check_client_sources(names):
    """`names` as a tuple, or ValueError naming an unknown or repeated source."""
    names = tuple(names)
    unknown = [n for n in names if n not in CLIENT_SOURCES]
    if unknown:
        raise ValueError(f"unknown client source(s) {unknown} in CLIENT_SOURCES; known: {list(CLIENT_SOURCES)}")
    if len(set(names)) != len(names):
        raise ValueError(f"CLIENT_SOURCES lists a source twice: {list(names)}")
    return names


def client_sources_note(sources, implicit=False):
    """One line saying which optional client sources a build includes (and why, if that was a guess)."""
    if not sources:
        return "[build] client sources: none (core only; list them in CLIENT_SOURCES in local_paths.py)"
    note = f"[build] client sources: {', '.join(sources)}"
    if implicit:
        note += ("  (assumed: local_paths.py defines the music inputs but no CLIENT_SOURCES; add "
                 f"CLIENT_SOURCES = {tuple(sources)!r} to make this explicit)")
    return note


def _wanted(source, enabled):
    return source is None or any(n in enabled for n in ((source,) if isinstance(source, str) else source))


def steps_for(enabled):
    """The step files to run, in order, for the enabled client sources."""
    enabled = check_client_sources(enabled)
    return [path for path, source in PIPELINE if _wanted(source, enabled)]


def schema_files(enabled):
    """The schema files to apply, in order: schema.sql, then the fragment of each enabled source."""
    enabled = check_client_sources(enabled)
    return ["schema.sql"] + [path for path, needed_by in SCHEMA_FRAGMENTS if _wanted(needed_by, enabled)]


def missing_paths(enabled, defined):
    """[(source, local_paths name)] for enabled sources whose input path is not set. `defined` is the set of
    names that have a value."""
    enabled = check_client_sources(enabled)
    return [(n, REQUIRED_PATHS[n]) for n in enabled if n in REQUIRED_PATHS and REQUIRED_PATHS[n] not in set(defined)]


def report_tables(existing):
    """REPORT_TABLES that exist in the built db (a client without the music source has no `artists`)."""
    return [t for t in REPORT_TABLES if t in set(existing)]


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
    scrobbles pushed a routine backup from a few hundred KB to 20-45MB, so leaving these unpruned turns every
    rebuild into unbounded disk growth."""
    backups = sorted((f for f in names if f.startswith(BACKUP_PREFIX)), key=_backup_order, reverse=True)
    return backups[keep:]
