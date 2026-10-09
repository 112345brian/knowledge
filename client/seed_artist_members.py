"""Seed artist_members from the curated ARTIST_MEMBERS dict in artist_rules.py --
e.g. a group or collaborative credit
stay their own artist row (see schema.sql's comment on artist_members for
why splitting per-credit was tried and reverted), and this records which
real artists are members of them as a separate, queryable fact.

Runs after the artist imports so the member artists already exist
from their own solo credits rather than being created fresh here with
nothing else attached.
"""
import sqlite3, os
from client.music_shared import load_artist_cache, seed_artist_members


def run(con):
    cur = con.cursor()
    artist_cache = load_artist_cache(cur)
    inserted = seed_artist_members(cur, artist_cache)
    con.commit()
    print(f"[seed_artist_members] inserted {inserted} artist_members rows")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
