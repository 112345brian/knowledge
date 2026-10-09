"""Build pipeline rules, domain: no file, no db, no clock.

The order of the ingest steps, which backups to prune, what a backup is called and which tables the build
report counts. `build.py` is the driving adapter that runs the steps and swaps the database in.
"""

KEEP_BACKUPS = 5
BACKUP_PREFIX = "knowledge.db.bak-"

STEPS = [
    "seed_sources.py",
    "literature_sources.py",
    "measurements.py",
    "facts.py",
    "seed_claims.py",
    "seed_subject_hierarchy.py",
    "concerts.py",
    "music_ratings.py",
    "scrobbles.py",
    "seed_artist_members.py",
    "seed_general_facts.py",
    "apply_fact_revisions.py",
    "link_entities.py",
    "link_source_relations.py",
]

REPORT_TABLES = ("sources", "authors", "source_authors", "publishers", "metrics", "measurements", "facts", "subjects",
                 "vault_files", "source_identifiers", "build_info", "build_inputs", "source_relations", "entities", "entity_aliases", "fact_entities", "claims", "claim_facts", "fact_sources", "fact_revisions", "fact_measurements",
                 "exercises", "training_sets", "foods", "food_log_entries", "meal_log_entries",
                 "muscles", "muscle_volume_weekly",
                 "artists", "artist_members", "venues", "festivals", "concert_attendances", "albums",
                 "tracks", "import_sources", "scrobbles")


def backup_name(now):
    """The file name of a backup taken at the datetime `now`."""
    return f"{BACKUP_PREFIX}{now:%Y%m%dT%H%M%S}"


def backups_to_prune(names, keep=KEEP_BACKUPS):
    """Of the backup file names `names`, the ones to delete so only the `keep` most recent remain --
    scrobbles pushed a routine backup from a few hundred KB to 20-45MB, so leaving these unpruned turns every
    rebuild into unbounded disk growth."""
    backups = sorted((f for f in names if f.startswith(BACKUP_PREFIX)), reverse=True)
    return backups[keep:]
