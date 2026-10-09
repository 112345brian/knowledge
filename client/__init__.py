"""Optional client sources: data a given client may or may not have, and the code that ingests it.

Each source is enabled per checkout with `CLIENT_SOURCES` in local_paths.py (see paths.py and build_rules.py).
The core pipeline (`ingest/`) builds a complete knowledge.db without any of them; a source adds its own tables
(`client/<source>.sql`, applied after schema.sql) and its own build steps.

    concerts      a concerts CSV                                  (client/concerts.py)
    ratings       a RateYourMusic ratings export                  (client/music_ratings.py)
    scrobbles     a Last.fm scrobbles export                      (client/scrobbles.py)
    measurements  a health/fitness vault's numeric readings       (client/measurements.py, link_fact_measurements.py)
    claims        hand-authored claims from the private data directory (client/seed_claims.py)

The three music sources share client/music.sql (the artists tables).

Nothing in `ingest/` imports from here. Run a standalone tool as `python3 -m client.<name>`.
"""
