"""Load general_facts.json (in the private data directory, not this repo): ad hoc
facts with no separate source database behind them (no external source database, no
sources/*.md frontmatter, often no source at all) -- the stray thing you
noticed and want on record. Add entries via add_fact.py, which enforces the
shape this script expects; don't hand-edit the JSON.

New subjects created here default to domain='general' rather than the
health-and-fitness default other scripts rely on.

The decisions and the SQL are the fact ingest's (fact_ingest_service over the fact repository, the same
implementation facts.py runs); this step hands it the connection and prints the report.

Run last -- has no dependency on anything but schema.sql, but nothing else
depends on it either.
"""
import fact_ingest
from fact_ingest_decisions import render


def run(con, ctx):
    for line in render(fact_ingest.compose(ctx, con).ingest_general_facts(con, warn=print)):
        print(line)
