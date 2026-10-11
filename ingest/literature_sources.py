"""Parse every citation note's (sources/*.md, see paths.py -> NOTES_VAULT)
frontmatter into a `sources` row. Mechanical: structured frontmatter -> low
risk of misreading. Run after seed_sources.py.
"""
import source_ingest_service
from ingest import source_readers


def run(con, ctx):
    sources = ctx.stores.source_repository(con)
    records = source_readers.VaultNotes(ctx.inputs, ctx.locations).records()
    inserted = source_ingest_service.add_sources(sources, records, skip_known=True, report=print)
    con.commit()
    print(f"[literature_sources] parsed {len(records)} files, inserted {inserted} new sources")
