"""Optional client sources: data a given client may or may not have, and the code that ingests it.

Each source is enabled per checkout with `CLIENT_SOURCES` in local_paths.py (see paths.py and build_rules.py).
The core pipeline (`ingest/`) builds a complete knowledge.db without any of them; a source adds its own tables
(`client/<source>.sql`, applied after schema.sql) and its own build steps.

    music         concerts, album ratings, scrobbles                (concerts, music_ratings, scrobbles, seed_artist_members)
    measurements  a health/fitness vault's numeric readings
    claims        hand-authored claims citing facts

Nothing in `ingest/` imports from here. Run a standalone tool as `python3 -m client.<name>`.
"""
