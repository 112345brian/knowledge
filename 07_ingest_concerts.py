"""Ingest <CONCERTS_CSV> into the normalized
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
import sqlite3, csv, os, re

CSV_PATH = os.path.expanduser("<CONCERTS_CSV>")

KNOWN_FESTIVALS = {
    "Camp Flog Gnaw", "Flog Gnaw", "Coachella", "Bonnaroo", "Primavera Sound",
    "Second Sky", "This Ain't No Picnic", "Boiler Room", "Portola",
    "Best Friends Forever", "No Values",  # a real Goldenvoice punk festival
}
FESTIVAL_NOTE_RE = re.compile(r"festival", re.IGNORECASE)
OPENER_RE = re.compile(r"open(?:ed|ing)?\s+for\s+(.+?)(?:;|$)", re.IGNORECASE)


def parse_location(location):
    """Split 'Venue, City, ST' -> ('Venue', 'City, ST'); no comma -> (location, None)."""
    if not location:
        return None, None
    if "," in location:
        venue, rest = location.split(",", 1)
        return venue.strip() or None, rest.strip() or None
    return location, None


def parse_row(name, location, notes):
    is_festival = bool(FESTIVAL_NOTE_RE.search(notes or ""))
    festival_name = None
    venue, city_state = None, None

    if name in KNOWN_FESTIVALS:
        is_festival = True
        festival_name = name
        venue, city_state = parse_location(location)  # Location is the sub-venue/city for the festival's own row
    elif location in KNOWN_FESTIVALS:
        is_festival = True
        festival_name = location
        # Location was consumed as the festival name, not an actual venue
    else:
        venue, city_state = parse_location(location)

    opener_match = OPENER_RE.search(notes or "")
    if opener_match:
        billing, supporting_for = "opener", opener_match.group(1).strip()
    elif is_festival:
        billing, supporting_for = "festival-set", None
    else:
        billing, supporting_for = "headliner", None

    return venue, city_state, festival_name, billing, supporting_for


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
    cur.execute("DELETE FROM concert_attendances WHERE source_file = ?", (CSV_PATH,))

    inserted = 0
    unresolved_festival_names = 0
    billing_counts = {}

    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = row["Concert"].strip()
            start_date = row["Start Date"].strip()
            end_date = row["End Date"].strip() or None
            location = row["Location"].strip() or None
            notes = row["Notes"].strip() or None
            if not name or not start_date:
                continue

            venue, city_state, festival_name, billing, supporting_for = parse_row(name, location, notes)
            if FESTIVAL_NOTE_RE.search(notes or "") and not festival_name:
                unresolved_festival_names += 1
            billing_counts[billing] = billing_counts.get(billing, 0) + 1

            artist_id = get_or_create(cur, "artists", name=name)
            venue_id = get_or_create(cur, "venues", name=venue, city_state=city_state) if venue else None
            festival_id = get_or_create(cur, "festivals", name=festival_name) if festival_name else None
            supporting_id = get_or_create(cur, "artists", name=supporting_for) if supporting_for else None

            cur.execute(
                """INSERT INTO concert_attendances (artist_id, start_date, end_date, venue_id, festival_id,
                                                      billing, supporting_for_artist_id, notes, domain, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'music', ?)""",
                (artist_id, start_date, end_date, venue_id, festival_id, billing, supporting_id, notes, CSV_PATH),
            )
            inserted += 1

    con.commit()
    print(f"[07_ingest_concerts] inserted {inserted} concert_attendances")
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
