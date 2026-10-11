"""Ingest a Last.fm scrobble export into `tracks`/`scrobbles`, reusing
`artists` (shared with concert_attendances/albums) for artist identity.

The export format is a list of *pages* (one per API call during the original
scrape), each page a list of track dicts -- not a flat list of scrobbles.
Flattening that is one trick; the other is that repeated text isn't stored
per play -- 153k scrobbles collapse to ~30k distinct tracks (avg 5
plays/track), so each gets one `tracks` row; and the source file path
(previously repeated as ~80 bytes of text on all 153k rows, 12MB+ of pure
duplication) gets one `import_sources` row instead. Same get-or-create /
delete-by-source idempotency pattern as concerts.py and
music_ratings.py otherwise.

At ~150k rows this is far bigger than any other single ingest in this db, so
artist and track lookups are batched through in-memory caches instead of one
SELECT per row, and rows are inserted with executemany.

Scrobble exports may represent group or collaborative credits as one artist
string. There's no safe general rule for splitting those credits, so each
string becomes its own artist row. Membership facts are curated separately
in artist_members, not derived here.

NOTE: iter_scrobbles() and the artist-cache helpers are duplicated in a
separate importer. The two tools are independent, so a parsing fix here may
need to be made there too.
"""
import sqlite3, json, os
from client.music_shared import load_artist_cache, get_or_create_artist, load_artist_rules
from ingest.shared import get_or_create
from client import music_ingest_rules


def iter_scrobbles(inputs, path):
    pages = inputs.read_json(path)
    yield from music_ingest_rules.scrobbles_from_pages(pages)


def get_or_create_track(cur, cache, artist_id, title, album):
    key = (artist_id, title, album)
    if key in cache:
        return cache[key]
    cur.execute("INSERT INTO tracks (artist_id, title, album) VALUES (?, ?, ?)", key)
    cache[key] = cur.lastrowid
    return cache[key]


def run(con, ctx):
    cur = con.cursor()

    if not ctx.inputs.exists(ctx.locations.scrobbles_json):
        print(f"[scrobbles] {ctx.locations.scrobbles_json} not found, skipping")
        return

    import_source_id = get_or_create(cur, "import_sources", "path", ctx.locations.scrobbles_json)
    cur.execute("DELETE FROM scrobbles WHERE import_source_id = ?", (import_source_id,))

    scrobbles = list(iter_scrobbles(ctx.inputs, ctx.locations.scrobbles_json))

    artist_cache = load_artist_cache(cur)
    rules = load_artist_rules(ctx.inputs, ctx.locations.private_data_dir)
    before_artists = len(artist_cache)
    artist_ids = {}
    for artist, *_ in scrobbles:
        if artist not in artist_ids:
            artist_ids[artist] = get_or_create_artist(cur, artist_cache, artist, rules)
    new_artists = len(artist_cache) - before_artists

    track_cache = {
        (artist_id, title, album): id_
        for id_, artist_id, title, album in cur.execute("SELECT id, artist_id, title, album FROM tracks")
    }
    before_tracks = len(track_cache)
    rows = []
    for artist, track, album, played_at in scrobbles:
        track_id = get_or_create_track(cur, track_cache, artist_ids[artist], track, album)
        rows.append((track_id, played_at, import_source_id))
    new_tracks = len(track_cache) - before_tracks

    cur.executemany(
        "INSERT OR IGNORE INTO scrobbles (track_id, played_at, import_source_id) VALUES (?, ?, ?)",
        rows,
    )

    con.commit()
    print(f"[scrobbles] parsed {len(scrobbles)} scrobbles, {new_artists} new artists, {new_tracks} new tracks")
    print(f"  scrobbles: {cur.execute('SELECT COUNT(*) FROM scrobbles').fetchone()[0]}, "
          f"tracks: {cur.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]}, "
          f"artists: {cur.execute('SELECT COUNT(*) FROM artists').fetchone()[0]}")
