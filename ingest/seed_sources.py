"""Hand-authored/manual sources: the original pilot sources plus small
manual instruments (tape measurements, the strength-checkpoint script, and
the one deliberately-unlocatable CRP/ESR carry-forward) that don't come from
parsing sources/*.md frontmatter (that's literature_sources.py) or
from a vault's raw tables (that's the measurements client source, which
creates its own vault-db-* sources for the tables it reads).

The actual citations (dates, file names, descriptions -- personal content)
live in knowledge-private/data/manual_sources.json, not here: this script is
just the generic loading mechanism. An origin_path in that JSON may contain
'{VAULT}' or '{HEALTH}' placeholders, substituted from the configured locations
(see source_readers.ManualSources).

Run after schema.sql, before 02/03/04.
"""
import source_ingest_service
from ingest import source_readers


def run(con, ctx):
    sources = ctx.stores.source_repository(con)
    records = source_readers.ManualSources(ctx.inputs, ctx.locations).records()
    source_ingest_service.add_sources(sources, records, report=print)
    con.commit()
    print(f"[seed_sources] inserted {len(records)} manual sources")
