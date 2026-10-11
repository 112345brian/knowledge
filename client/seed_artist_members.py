"""Seed artist_members from the curated `members` of artist_rules.json -- e.g. a group or
collaborative credit stays its own artist row (see schema.sql's comment on artist_members for
why splitting per-credit was tried and reverted), and this records which
real artists are members of them as a separate, queryable fact.

Runs after the artist imports so the member artists already exist
from their own solo credits rather than being created fresh here with
nothing else attached.

The work is `music_ingest_service.seed_artist_members`; this step composes it for the build
connection and prints what it reports.
"""
import music_ingest


def run(con, ctx):
    result = music_ingest.compose(con, ctx.inputs, ctx.locations).seed_artist_members()
    for line in result.lines:
        print(line)
