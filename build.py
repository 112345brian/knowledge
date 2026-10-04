#!/usr/bin/env python3
"""Rebuild ~/MEGA/library/knowledge.db from scratch: schema + every
seed/ingest script in this directory, in order. This is the ONLY sanctioned
way to change knowledge.db's structure or bulk contents -- edit a script
here and rerun, never ALTER/INSERT by hand against the live file.

Usage:
    python3 build.py            # rebuilds knowledge.db (backs up the old one first)
    python3 build.py --check    # builds into a temp file and reports counts, doesn't touch the live DB

The rebuild goes into a temp file next to the live DB and is os.replace()d onto
it only after every step succeeds, so a failing step (exit 1, naming the step)
leaves the previous knowledge.db byte-identical, and a reader holding the old
file open keeps a consistent snapshot.
"""
import sqlite3, os, sys, shutil, datetime, importlib.util, tempfile

from paths import KNOWLEDGE_DB_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.expanduser(KNOWLEDGE_DB_DIR)
LIVE_DB = os.path.join(DB_DIR, "knowledge.db")
SCHEMA = os.path.join(HERE, "schema.sql")
BACKUP_DIR = os.path.join(DB_DIR, "backups")
KEEP_BACKUPS = 5

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


def load_module(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuildError(Exception):
    """A build stage failed; `stage` names it (a step filename or 'schema')."""
    def __init__(self, stage, cause):
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause


def build(target_path):
    if os.path.exists(target_path):
        os.remove(target_path)
    con = sqlite3.connect(target_path)
    try:
        con.execute("PRAGMA foreign_keys = ON;")
        try:
            with open(SCHEMA) as f:
                con.executescript(f.read())
        except Exception as e:
            raise BuildError("schema", e) from e
        for step in STEPS:
            try:
                mod = load_module(os.path.join(HERE, step))
                mod.run(con)
            except Exception as e:
                raise BuildError(step, e) from e
        con.execute("VACUUM;")
        con.execute("ANALYZE;")
    finally:
        con.close()


def build_to_temp(directory):
    """Build into a fresh unique temp file in `directory`; return its path.
    On failure the temp file is removed and BuildError propagates."""
    fd, tmp = tempfile.mkstemp(prefix="knowledge.db.building-", dir=directory)
    os.close(fd)
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(tmp, 0o666 & ~umask)  # mkstemp makes it 0600; keep the mode a plain sqlite3.connect gives
    try:
        build(tmp)
    except BaseException:
        remove_db_files(tmp)
        raise
    return tmp


def remove_db_files(path):
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def prune_backups(keep=KEEP_BACKUPS):
    """Keep only the `keep` most recent backups -- scrobbles pushed a
    routine backup from a few hundred KB to 20-45MB, so leaving these
    unpruned turns every rebuild into unbounded disk growth."""
    backups = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith("knowledge.db.bak-")),
        reverse=True,
    )
    for old in backups[keep:]:
        os.remove(os.path.join(BACKUP_DIR, old))
        print(f"  pruned old backup {old}")


def report(path):
    con = sqlite3.connect(path)
    cur = con.cursor()
    for table in ("sources", "authors", "source_authors", "publishers", "metrics", "measurements", "facts", "subjects",
                  "vault_files", "claims", "claim_facts", "fact_sources", "fact_revisions", "fact_measurements",
                  "exercises", "training_sets", "foods", "food_log_entries", "meal_log_entries",
                  "muscles", "muscle_volume_weekly",
                  "artists", "artist_members", "venues", "festivals", "concert_attendances", "albums",
                  "tracks", "import_sources", "scrobbles"):
        n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n}")
    con.close()
    print(f"  file size: {os.path.getsize(path) / 1_000_000:.1f} MB")


def main():
    check_only = "--check" in sys.argv
    try:
        if check_only:
            print("Building into a temp file (live DB untouched)...")
            tmp = build_to_temp(tempfile.gettempdir())
            try:
                report(tmp)
            finally:
                remove_db_files(tmp)
            return

        os.makedirs(DB_DIR, exist_ok=True)
        print(f"Rebuilding {LIVE_DB}...")
        tmp = build_to_temp(DB_DIR)
    except BuildError as e:
        print(f"error: build failed in {e.stage}: {e.cause!r}; live DB left untouched", file=sys.stderr)
        sys.exit(1)

    try:
        if os.path.exists(LIVE_DB):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            backup = os.path.join(BACKUP_DIR, f"knowledge.db.bak-{datetime.datetime.now():%Y%m%dT%H%M%S}")
            shutil.copy2(LIVE_DB, backup)
            print(f"Backed up existing DB to {backup}")
            prune_backups()
        os.replace(tmp, LIVE_DB)
    except BaseException:
        remove_db_files(tmp)
        raise
    report(LIVE_DB)


if __name__ == "__main__":
    main()
