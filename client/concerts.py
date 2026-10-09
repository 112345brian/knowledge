"""Ingest the concerts export CSV (see paths.py -> CONCERTS_CSV) into the normalized
artists / venues / festivals / concert_attendances tables. Concert-going is
neither a `facts` claim (no trust/provenance dimension) nor a `measurements`
metric (not a scalar) -- its own shape. And within that shape, artist/venue/
festival are entities worth their own tables, not text repeated on every row:
that's what makes "how many times have I seen X" or "every act at festival Y"
a join instead of a string match.

Parses the source's free-text Location/Notes into structured fields -- see
inline comments for the exact rules. Idempotent: re-running deletes and
re-inserts this source file's own attendances (and any artists/venues/
festivals become orphaned only if nothing else references them, which is
fine -- they're just not re-created if already present).
"""
import sqlite3, csv, os
from client.music_shared import load_artist_cache, get_or_create_artist, load_artist_rules
from client import music_ingest_rules
from paths import CONCERTS_CSV

CSV_PATH = os.path.expanduser(CONCERTS_CSV)

def get_or_create(cur, table, **fields):
    where = " AND ".join(f"{k} IS :{k}" for k in fields)
    row = cur.execute(f"SELECT id FROM {table} WHERE {where}", fields).fetchone()
    if row:
        return row[0]
    cols = ", ".join(fields)
    placeholders = ", ".join(f":{k}" for k in fields)
    cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", fields)
    return cur.lastrowid


def run(con):
    cur = con.cursor()
    import_source_id = get_or_create(cur, "import_sources", path=CSV_PATH)
    cur.execute("DELETE FROM concert_attendances WHERE import_source_id = ?", (import_source_id,))
    artist_cache = load_artist_cache(cur)
    known_festivals = load_artist_rules().get("festivals", [])

    inserted = 0
    unresolved_festival_names = 0
    billing_counts = {}

    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            concert = music_ingest_rules.concert_from_csv(row, known_festivals)
            if concert is None:
                continue
            name, start_date, end_date, notes = concert["name"], concert["start_date"], concert["end_date"], concert["notes"]
            venue, city_state, festival_name = concert["venue"], concert["city_state"], concert["festival_name"]
            billing, supporting_for = concert["billing"], concert["supporting_for"]
            if concert["unresolved_festival"]:
                unresolved_festival_names += 1
            billing_counts[billing] = billing_counts.get(billing, 0) + 1

            artist_id = get_or_create_artist(cur, artist_cache, name)
            venue_id = get_or_create(cur, "venues", name=venue, city_state=city_state) if venue else None
            festival_id = get_or_create(cur, "festivals", name=festival_name) if festival_name else None
            supporting_id = get_or_create_artist(cur, artist_cache, supporting_for) if supporting_for else None

            cur.execute(
                """INSERT INTO concert_attendances (artist_id, start_date, end_date, venue_id, festival_id,
                                                      billing, supporting_for_artist_id, notes, domain, import_source_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'music', ?)""",
                (artist_id, start_date, end_date, venue_id, festival_id, billing, supporting_id, notes, import_source_id),
            )
            inserted += 1

    con.commit()
    print(f"[concerts] inserted {inserted} concert_attendances")
    print(f"  artists: {cur.execute('SELECT COUNT(*) FROM artists').fetchone()[0]}, "
          f"venues: {cur.execute('SELECT COUNT(*) FROM venues').fetchone()[0]}, "
          f"festivals: {cur.execute('SELECT COUNT(*) FROM festivals').fetchone()[0]}")
    print(f"  festival rows with no resolvable festival name: {unresolved_festival_names} (Location held a bare city or similar -- not guessed)")
    for b, n in billing_counts.items():
        print(f"  billing={b}: {n}")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
