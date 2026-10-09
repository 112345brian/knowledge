"""Build pipeline rules, domain: no file, no db, no clock.

The order of the ingest steps, which backups to prune, what a backup is called and which tables the build
report counts. `build.py` is the driving adapter that runs the steps and swaps the database in.
"""

KEEP_BACKUPS = 5
BACKUP_PREFIX = "knowledge.db.bak-"

STEPS = [
    "01_seed_sources.py",
    "02_ingest_literature_sources.py",
    "03_ingest_measurements.py",
    "04_ingest_facts.py",
    "05_seed_claims.py",
    "06_seed_subject_hierarchy.py",
    "07_ingest_concerts.py",
    "08_ingest_music_ratings.py",
    "09_ingest_scrobbles.py",
    "10_seed_artist_members.py",
    "11_seed_general_facts.py",
    "12_apply_fact_revisions.py",
]

REPORT_TABLES = ("sources", "authors", "source_authors", "publishers", "metrics", "measurements", "facts", "subjects",
                 "vault_files", "claims", "claim_facts", "fact_sources", "fact_revisions", "fact_measurements",
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
