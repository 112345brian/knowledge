"""Parse every citation note's (sources/*.md, see paths.py -> BODYBUILDING_VAULT)
frontmatter into a `sources` row. Mechanical: structured frontmatter -> low
risk of misreading. Run after 01_seed_sources.py.
"""
import sqlite3, os, glob, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import link_authors, get_or_create_publisher
import source_ingest_rules
from paths import BODYBUILDING_VAULT as VAULT

SRC_DIR = os.path.expanduser(f"{VAULT}/sources")

def parse_all():
    rows = []
    for fp in sorted(glob.glob(os.path.join(SRC_DIR, "*.md"))):
        base = os.path.basename(fp)
        if base == "README.md":
            continue
        text = open(fp, encoding="utf-8").read()
        rows.append(source_ingest_rules.note_to_source_row(base, text, VAULT))
    return rows


def run(con):
    rows = parse_all()
    cur = con.cursor()
    existing = {r[0] for r in cur.execute("SELECT citekey FROM sources WHERE citekey IS NOT NULL")}
    inserted = 0
    for r in rows:
        if r["citekey"] in existing:
            continue
        row = {k: v for k, v in r.items() if k not in ("author", "publisher")}
        row["publisher_id"] = get_or_create_publisher(cur, r.get("publisher"))
        cur.execute(
            """INSERT INTO sources (citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, origin_path)
               VALUES (:citekey, :name, :source_type, :publisher_id, :url, :published_date, '2026-09-11', :description, :origin_path)""",
            row,
        )
        link_authors(cur, cur.lastrowid, r["author"])
        existing.add(r["citekey"])
        inserted += 1
    con.commit()
    print(f"[02_ingest_literature_sources] parsed {len(rows)} files, inserted {inserted} new sources")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
