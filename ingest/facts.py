"""Load pilot_facts.json (hand-authored) plus facts_batch1-4.json (extracted by
background agents reading the vault's notes) into `facts`, `fact_sources`, `fact_subjects`. (The `measurements` client source then links facts to
measurements: client/link_fact_measurements.py.)

Classifies is_personal with a multi-signal heuristic (the generic pronoun rule plus the author's fact_hints.json,
see fact_ingest_rules.Hints) -- a best-effort first pass, not hand-verified per fact.

The decisions and the SQL are the fact ingest's (fact_ingest_service over the fact repository); this step hands
it the connection and prints the report. seed_general_facts.py runs the same implementation over general_facts.json.

Run after seed_sources and literature_sources (needs sources + subjects to exist).
"""
import fact_ingest
from fact_ingest_decisions import render


def load_items(ctx):
    """All entries, each with `_is_pilot`, `_where` (file, index) and `_source_key` (its own, or the deterministic
    legacy-... key from revisions.derive_keys until backfill_source_keys.py writes one)."""
    items = []
    for entry in fact_ingest.compose(ctx).load_fact_file_entries():
        item = entry.item
        item["_is_pilot"] = entry.is_pilot
        item["_source_key"] = entry.key
        item["_where"] = entry.where
        items.append(item)
    return items


def run(con, ctx):
    for line in render(fact_ingest.compose(ctx).ingest_fact_files(con)):
        print(line)
