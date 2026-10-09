"""Load source-to-source relations (#41): `replaces` and `is-version-of`.

Relations come from the same two places as the sources themselves: the `replaces:` / `is-version-of:` lists in a
vault note's frontmatter (02) and the `replaces` / `is_version_of` lists in manual_sources.json (01). They are
loaded here, after BOTH have inserted their sources, because a manual source may replace a vault source (the pilot
data already says a textbook's "3rd edition will supersede this"), which no single earlier step can resolve.

Validation (a violation fails the build and names the sources): every citekey must exist, a source may not relate
to itself, and neither relation may form a cycle. Duplicate triples are collapsed. No relations anywhere is fine.

Run after 02 (which parses the notes; this reuses its parser rather than keeping a second one).
"""
import importlib.util
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import source_status


def _load(filename):
    spec = importlib.util.spec_from_file_location(filename, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect():
    """[(source citekey, relation, related citekey)] from manual_sources.json and the vault notes."""
    rels = []
    for s in _load("01_seed_sources.py").load_sources():
        for rel, key in (("replaces", "replaces"), ("is-version-of", "is_version_of")):
            rels.extend((s["citekey"], rel, t) for t in source_status.as_list(s.get(key)))
    rels.extend(r for row in _load("02_ingest_literature_sources.py").parse_all() for r in row["relations"])
    return rels


def run(con):
    ids = {citekey: sid for citekey, sid in con.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}
    rels = source_status.check_relations(collect(), ids)
    for a, rel, b in rels:
        con.execute("INSERT INTO source_relations (source_id, relation, related_source_id) VALUES (?, ?, ?)", (ids[a], rel, ids[b]))
    con.commit()
    counts = dict(con.execute("SELECT status, COUNT(*) FROM sources GROUP BY status"))
    print(f"[14_link_source_relations] {len(rels)} relations; sources by status: {counts}")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(HERE, "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
