"""Build pipeline rules, domain: no file, no db, no clock.

The order of the build steps and which of them a checkout runs, which backups to prune, what a backup is called and which tables the build
report counts. `build.py` is the driving adapter that runs the steps and swaps the database in.
"""

KEEP_BACKUPS = 5
BACKUP_PREFIX = "knowledge.db.bak-"

# The optional client sources: data a given client may or may not have and want in knowledge.db. A checkout
# enables the ones it uses with CLIENT_SOURCES in local_paths.py (see paths.py); the core pipeline needs none.
#   music         concerts, album ratings, scrobbles           (client/music.sql)
#   measurements  a health/fitness vault's numeric readings   (client/measurements.sql)
#   claims        hand-authored claims citing facts
CLIENT_SOURCES = ("music", "measurements", "claims")
SCHEMA_FRAGMENTS = {"music": "client/music.sql", "measurements": "client/measurements.sql"}

# Every build step in run order, with the client source it belongs to (None = core). Paths are relative to the
# repo root. A step runs when its source is None or enabled; the order is the same either way.
PIPELINE = (
    ("ingest/seed_sources.py", None),
    ("ingest/literature_sources.py", None),
    ("client/measurements.py", "measurements"),
    ("ingest/facts.py", None),
    ("client/link_fact_measurements.py", "measurements"),
    ("ingest/seed_claims.py", "claims"),
    ("ingest/seed_subject_hierarchy.py", None),
    ("client/concerts.py", "music"),
    ("client/music_ratings.py", "music"),
    ("client/scrobbles.py", "music"),
    ("client/seed_artist_members.py", "music"),
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


def steps_for(enabled):
    """The step files to run, in order, for the enabled client sources."""
    enabled = check_client_sources(enabled)
    return [path for path, source in PIPELINE if source is None or source in enabled]


def schema_files(enabled):
    """The schema files to apply, in order: schema.sql, then the fragment of each enabled source."""
    enabled = check_client_sources(enabled)
    return ["schema.sql"] + [SCHEMA_FRAGMENTS[n] for n in CLIENT_SOURCES if n in enabled and n in SCHEMA_FRAGMENTS]


def report_tables(existing):
    """REPORT_TABLES that exist in the built db (a client without the music source has no `artists`)."""
    return [t for t in REPORT_TABLES if t in set(existing)]


def backup_name(now):
    """The file name of a backup taken at the datetime `now`."""
    return f"{BACKUP_PREFIX}{now:%Y%m%dT%H%M%S}"


def backups_to_prune(names, keep=KEEP_BACKUPS):
    """Of the backup file names `names`, the ones to delete so only the `keep` most recent remain --
    scrobbles pushed a routine backup from a few hundred KB to 20-45MB, so leaving these unpruned turns every
    rebuild into unbounded disk growth."""
    backups = sorted((f for f in names if f.startswith(BACKUP_PREFIX)), reverse=True)
    return backups[keep:]
