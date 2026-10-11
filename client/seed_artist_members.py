"""Seed artist_members from the curated ARTIST_MEMBERS dict in artist_rules.py --
e.g. a group or collaborative credit
stay their own artist row (see schema.sql's comment on artist_members for
why splitting per-credit was tried and reverted), and this records which
real artists are members of them as a separate, queryable fact.

Runs after the artist imports so the member artists already exist
from their own solo credits rather than being created fresh here with
nothing else attached.
"""
from client.music_shared import load_artist_cache, load_artist_rules, seed_artist_members


def run(con, ctx):
    cur = con.cursor()
    artist_cache = load_artist_cache(cur)
    inserted = seed_artist_members(cur, artist_cache, load_artist_rules(ctx.inputs, ctx.locations.private_data_dir))
    con.commit()
    print(f"[seed_artist_members] inserted {inserted} artist_members rows")
