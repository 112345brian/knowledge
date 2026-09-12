#!/usr/bin/env python3
"""Rebuild ~/MEGA/library/knowledge.db from scratch: schema + every
seed/ingest script in this directory, in order. This is the ONLY sanctioned
way to change knowledge.db's structure or bulk contents -- edit a script
here and rerun, never ALTER/INSERT by hand against the live file.

Usage:
    python3 build.py            # rebuilds knowledge.db in place (backs up the old one first)
    python3 build.py --check    # builds into a temp file and reports counts, doesn't touch the live DB
"""
import sqlite3, os, sys, shutil, datetime, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_DB = os.path.join(HERE, "knowledge.db")
SCHEMA = os.path.join(HERE, "schema.sql")
BACKUP_DIR = os.path.join(HERE, "backups")

STEPS = [
    "01_seed_sources.py",
    "02_ingest_literature_sources.py",
    "03_ingest_measurements.py",
    "04_ingest_facts.py",
    "05_seed_claims.py",
    "06_seed_subject_hierarchy.py",
    "07_ingest_concerts.py",
]


def load_module(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(target_path):
    if os.path.exists(target_path):
        os.remove(target_path)
    con = sqlite3.connect(target_path)
    con.execute("PRAGMA foreign_keys = ON;")
    con.executescript(open(SCHEMA).read())
    for step in STEPS:
        mod = load_module(os.path.join(HERE, step))
        mod.run(con)
    con.execute("VACUUM;")
    con.close()


def report(path):
    con = sqlite3.connect(path)
    cur = con.cursor()
    for table in ("sources", "measurements", "facts", "subjects", "claims", "claim_facts", "fact_sources", "fact_measurements",
                  "artists", "venues", "festivals", "concert_attendances"):
        n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n}")
    con.close()
    print(f"  file size: {os.path.getsize(path) / 1_000_000:.1f} MB")


def main():
    check_only = "--check" in sys.argv

    if check_only:
        tmp = "/tmp/knowledge_db_build_check.db"
        print(f"Building into {tmp} (live DB untouched)...")
        build(tmp)
        report(tmp)
        os.remove(tmp)
        return

    if os.path.exists(LIVE_DB):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup = os.path.join(BACKUP_DIR, f"knowledge.db.bak-{datetime.datetime.now():%Y%m%dT%H%M%S}")
        shutil.copy2(LIVE_DB, backup)
        print(f"Backed up existing DB to {backup}")

    print(f"Rebuilding {LIVE_DB}...")
    build(LIVE_DB)
    report(LIVE_DB)


if __name__ == "__main__":
    main()
