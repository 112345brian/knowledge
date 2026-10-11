"""Parse every citation note's (sources/*.md, see paths.py -> NOTES_VAULT)
frontmatter into a `sources` row. Mechanical: structured frontmatter -> low
risk of misreading. Run after seed_sources.py.
"""
import sqlite3, os, glob, sys

import acquisition_store
import fixity_store
from ingest import source_ingest_rules
from ingest.shared import link_authors, get_or_create_publisher, collect_identifiers, add_source_identifiers


def parse_all(locations):
    rows = []
    if not locations.notes_vault:  # no notes vault configured: there are no citation notes to read
        return rows
    for fp in sorted(glob.glob(os.path.join(os.path.join(locations.notes_vault, "sources"), "*.md"))):
        base = os.path.basename(fp)
        if base == "README.md":
            continue
        text = open(fp, encoding="utf-8").read()
        row = source_ingest_rules.note_to_source_row(base, text, locations.notes_vault)
        where = row.pop("where")
        row["identifiers"] = collect_identifiers(row.pop("raw_identifiers"), where)
        row.update(acquisition_store.resolve(acquisition_store.normalize_data(row.pop("raw_acquired"), where), fp))  # #46
        row.update(fixity_store.fingerprint(fp))  # #38
        rows.append(row)
    return rows


def run(con, locations):
    rows = parse_all(locations)
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
    print(f"[literature_sources] parsed {len(rows)} files, inserted {inserted} new sources")
