import io
"""Ingest the RYM ratings export (see paths.py -> RYM_EXPORT_CSV) (a RateYourMusic
ratings export) into the `albums` table, reusing the `artists` table shared
with the concerts domain so an artist seen live and an artist rated on RYM
are the same row.

The export splits an artist's name into first and last name, with optional
localized fields. The localized name is preferred when present.

The export HTML-escapes special characters in both fields.
get_or_create_artist() unescapes artist names; the title field is unescaped
directly here.

Ownership/Purchase Date/Media Type columns are present in the export but
every row is blank/'n' -- no information to ingest, so they're dropped
rather than carried into empty columns.

Idempotent: re-running deletes and re-inserts this source file's own albums.
"""
import sqlite3, csv, os
from client.music_shared import load_artist_cache, get_or_create_artist, load_artist_rules
from ingest.shared import get_or_create
from client import music_ingest_rules


def run(con, ctx):
    cur = con.cursor()
    import_source_id = get_or_create(cur, "import_sources", "path", ctx.locations.rym_export_csv)
    cur.execute("DELETE FROM albums WHERE import_source_id = ?", (import_source_id,))
    artist_cache = load_artist_cache(cur)
    rules = load_artist_rules(ctx.inputs, ctx.locations.private_data_dir)

    inserted = 0
    with io.StringIO(ctx.inputs.read_text(ctx.locations.rym_export_csv, "utf-8-sig")) as f:
        for row in csv.DictReader(f):
            album = music_ingest_rules.album_from_row(row)
            if album is None:
                continue
            name, title, release_year, rating, rym_id = album

            artist_id = get_or_create_artist(cur, artist_cache, name, rules)
            cur.execute(
                """INSERT INTO albums (artist_id, title, release_year, rating, rym_id, import_source_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (artist_id, title, release_year, rating, rym_id, import_source_id),
            )
            inserted += 1

    con.commit()
    print(f"[music_ratings] inserted {inserted} albums")
    print(f"  artists: {cur.execute('SELECT COUNT(*) FROM artists').fetchone()[0]}")
