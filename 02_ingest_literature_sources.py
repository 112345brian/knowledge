"""Parse every citation note's (sources/*.md, see paths.py -> BODYBUILDING_VAULT)
frontmatter into a `sources` row. Mechanical: structured frontmatter -> low
risk of misreading. Run after 01_seed_sources.py.
"""
import sqlite3, os, glob, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acquisition_store
import fixity_store
import source_ingest_rules
from _shared import link_authors, get_or_create_publisher, collect_identifiers, add_source_identifiers
from paths import BODYBUILDING_VAULT as VAULT

SRC_DIR = os.path.expanduser(f"{VAULT}/sources")

def parse_all():
    rows = []
    for fp in sorted(glob.glob(os.path.join(SRC_DIR, "*.md"))):
        base = os.path.basename(fp)
        if base == "README.md":
            continue
        text = open(fp, encoding="utf-8").read()
        row = source_ingest_rules.note_to_source_row(base, text, VAULT)
        where = row.pop("where")
        row["identifiers"] = collect_identifiers(row.pop("raw_identifiers"), where)
        row.update(acquisition_store.resolve(acquisition_store.normalize_data(row.pop("raw_acquired"), where), fp))  # #46
        row.update(fixity_store.fingerprint(fp))  # #38
        rows.append(row)
    return rows


def run(con):
    rows = parse_all()
    cur = con.cursor()
    existing = {r[0] for r in cur.execute("SELECT citekey FROM sources WHERE citekey IS NOT NULL")}
    inserted = 0
    for r in rows:
        if r["citekey"] in existing:
            continue
        row = {k: v for k, v in r.items() if k not in ("author", "publisher", "relations", "identifiers")}
        row["publisher_id"] = get_or_create_publisher(cur, r.get("publisher"))
        cur.execute(
            """INSERT INTO sources (citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, origin_path,
                                    content_sha256, size_bytes, file_mtime, mime_type, file_state,
                                    status, status_date, status_note, edition, original_published_date,
                                    acquired_at, acquired_via, where_from, acquired_note)
               VALUES (:citekey, :name, :source_type, :publisher_id, :url, :published_date, '2026-09-11', :description, :origin_path,
                       :content_sha256, :size_bytes, :file_mtime, :mime_type, :file_state,
                       :status, :status_date, :status_note, :edition, :original_published_date,
                       :acquired_at, :acquired_via, :where_from, :acquired_note)""",
            row,
        )
        source_id = cur.lastrowid
        add_source_identifiers(cur, source_id, r["citekey"], r["identifiers"])
        link_authors(cur, source_id, r["author"])
        existing.add(r["citekey"])
        inserted += 1
    con.commit()
    print(f"[02_ingest_literature_sources] parsed {len(rows)} files, inserted {inserted} new sources")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
