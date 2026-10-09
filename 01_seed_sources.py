"""Hand-authored/manual sources: the original pilot sources plus small
manual instruments (tape measurements, the strength-checkpoint script, and
the one deliberately-unlocatable CRP/ESR carry-forward) that don't come from
parsing sources/*.md frontmatter (that's 02_ingest_literature_sources.py) or
from bodybuilding.db raw tables (that's 03_ingest_measurements.py, which
creates its own vault-db-* sources for the tables it reads).

The actual citations (dates, file names, descriptions -- personal content)
live in knowledge-private/data/manual_sources.json, not here: this script is
just the generic loading mechanism. An origin_path in that JSON may contain
'{VAULT}' or '{HEALTH}' placeholders, substituted from paths.py below.

Run after schema.sql, before 02/03/04.
"""
import sqlite3, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import link_authors, get_or_create_publisher
import source_ingest_rules
from paths import BODYBUILDING_VAULT as VAULT, HEALTH_DIR as HEALTH, PRIVATE_DATA_DIR

FIELDS = ("citekey", "name", "source_type", "author", "publisher", "url",
          "published_date", "retrieved_date", "description", "origin_path")


def load_sources():
    items = json.load(open(os.path.join(PRIVATE_DATA_DIR, "manual_sources.json")))
    for s in items:
        if s.get("origin_path"):
            s["origin_path"] = source_ingest_rules.format_origin_path(s["origin_path"], VAULT, HEALTH)
    return items


def run(con):
    cur = con.cursor()
    sources = load_sources()
    for s in sources:
        author = s.get("author")
        publisher_id = get_or_create_publisher(cur, s.get("publisher"))
        row = {k: s.get(k) for k in FIELDS if k not in ("author", "publisher")}
        row["publisher_id"] = publisher_id
        cur.execute(
            """INSERT INTO sources (citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, origin_path)
               VALUES (:citekey, :name, :source_type, :publisher_id, :url, :published_date, :retrieved_date, :description, :origin_path)""",
            row,
        )
        link_authors(cur, cur.lastrowid, author)
    con.commit()
    print(f"[01_seed_sources] inserted {len(sources)} manual sources")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
