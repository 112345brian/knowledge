"""Music ingest rules (steps 07, 08, 09 and clean_concerts_csv), domain: no file, no db.

How a concerts CSV row is split into venue / festival / billing, how an RYM export row becomes an album,
how Last.fm pages flatten into scrobbles, and how the concerts CSV is de-duplicated. The scripts read the
files and write the rows; they call these.
"""
import datetime
import html
import re

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


def parse_row(name, location, notes, known_festivals=()):
    is_festival = bool(FESTIVAL_NOTE_RE.search(notes or ""))
    festival_name = None
    venue, city_state = None, None

    if name in known_festivals:
        is_festival = True
        festival_name = name
        venue, city_state = parse_location(location)  # Location is the sub-venue/city for the festival's own row
    elif location in known_festivals:
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


def concert_from_csv(row, known_festivals=()):
    """The parsed concert for one CSV row (a dict), or None when it has no name or start date."""
    name = row["Concert"].strip()
    start_date = row["Start Date"].strip()
    end_date = row["End Date"].strip() or None
    location = row["Location"].strip() or None
    notes = row["Notes"].strip() or None
    if not name or not start_date:
        return None
    venue, city_state, festival_name, billing, supporting_for = parse_row(name, location, notes, known_festivals)
    return {"name": name, "start_date": start_date, "end_date": end_date, "notes": notes, "venue": venue,
            "city_state": city_state, "festival_name": festival_name, "billing": billing,
            "supporting_for": supporting_for,
            "unresolved_festival": bool(FESTIVAL_NOTE_RE.search(notes or "")) and not festival_name}


def album_artist_name(row):
    """RYM splits an artist's name into First/Last Name, with optional localized names for acts whose native
    name isn't English; the localized name is what a human would recognize, so it is preferred."""
    localized = f"{row['First Name localized'].strip()} {row[' Last Name localized'].strip()}".strip()
    if localized:
        return localized
    return f"{row[' First Name'].strip()} {row['Last Name'].strip()}".strip()


def album_from_row(row):
    """(artist name, title, release year, rating, rym id) for one RYM export row, or None when it has no
    artist or title. RYM HTML-escapes the title too, so it is unescaped here."""
    name = album_artist_name(row)
    title = html.unescape(row["Title"].strip())
    if not name or not title:
        return None
    return name, title, int(row["Release_Date"]), int(row["Rating"]), row["RYM Album"].strip()


def scrobbles_from_pages(pages):
    """Flatten a Last.fm export (a list of pages of track dicts) into (artist, track, album, played_at)
    tuples, skipping "now playing" entries and rows without an artist, track or timestamp."""
    for page in pages:
        for t in page:
            date = t.get("date")
            if not date:
                continue  # "now playing" entries from the API carry no date
            artist = (t.get("artist") or {}).get("#text", "").strip()
            track = (t.get("name") or "").strip()
            album = ((t.get("album") or {}).get("#text", "") or "").strip()
            uts = date.get("uts")
            if not artist or not track or not uts:
                continue
            played_at = datetime.datetime.fromtimestamp(int(uts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            yield artist, track, album, played_at


CONCERT_COLUMNS = ["Concert", "Start Date", "End Date", "Location", "Notes"]


def clean_concert_rows(rows):
    """The concerts export de-duplicated: stray whitespace stripped, the "NO VALUES" casing normalized, and
    near-duplicates (same name and date, one location a prefix of the other) merged keeping the more
    detailed location. Returns the merged rows."""
    cleaned = []
    for r in rows:
        c = {k: v.strip() for k, v in r.items()}
        # "NO VALUES" is the actual name of a real punk festival (Goldenvoice's
        # "No Values"), not a placeholder -- just normalize the casing.
        if c["Concert"] == "NO VALUES Festival":
            c["Concert"] = "No Values"
        if c["Location"] == "NO VALUES":
            c["Location"] = "No Values"
        cleaned.append(c)

    # merge near-duplicates: same (name, start_date), one location a prefix of the other
    merged = []
    for r in cleaned:
        match = next((m for m in merged
                      if m["Concert"] == r["Concert"] and m["Start Date"] == r["Start Date"]
                      and (m["Location"].startswith(r["Location"]) or r["Location"].startswith(m["Location"]))),
                     None)
        if match:
            if len(r["Location"]) > len(match["Location"]):
                match["Location"] = r["Location"]
            if not match["Notes"] and r["Notes"]:
                match["Notes"] = r["Notes"]
            if not match["End Date"] and r["End Date"]:
                match["End Date"] = r["End Date"]
        else:
            merged.append(dict(r))
    return merged
