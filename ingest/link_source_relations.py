"""Load source-to-source relations (#41): `replaces` and `is-version-of`.

Relations come from the same two places as the sources themselves: the `replaces:` / `is-version-of:` lists in a
vault note's frontmatter (02) and the `replaces` / `is_version_of` lists in manual_sources.json (01). They are
loaded here, after BOTH have inserted their sources, because a manual source may replace a vault source (the pilot
data already says a textbook's "3rd edition will supersede this"), which no single earlier step can resolve.

Validation (a violation fails the build and names the sources): every citekey must exist, a source may not relate
to itself, and neither relation may form a cycle. Duplicate triples are collapsed. No relations anywhere is fine.

Run after 02. The relations are read afresh from manual_sources.json and the notes (the composed readers of
source_readers, the same parsers 01 and 02 use), not carried over from those steps.
"""
import source_ingest_service
from ingest import source_readers


def collect(ctx):
    """[(source citekey, relation, related citekey)] from manual_sources.json and the vault notes, read afresh."""
    return source_readers.default_readers(ctx).relations()


def run(con, ctx):
    sources = ctx.stores.source_repository(con)
    rels = source_ingest_service.link_relations(sources, collect(ctx))
    con.commit()
    print(f"[link_source_relations] {len(rels)} relations; sources by status: {dict(sources.count_by_status())}")
