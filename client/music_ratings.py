"""Ingest the RYM ratings export (see paths.py -> RYM_EXPORT_CSV) (a RateYourMusic
ratings export) into the `albums` table, reusing the `artists` table shared
with the concerts domain so an artist seen live and an artist rated on RYM
are the same row.

The export splits an artist's name into first and last name, with optional
localized fields. The localized name is preferred when present.

The export HTML-escapes special characters in both fields. Artist names are unescaped when they are
resolved to an artist (artist_rules.canonical_artist_name); the title is unescaped in
music_ingest_rules.album_from_row.

Ownership/Purchase Date/Media Type columns are present in the export but
every row is blank/'n' -- no information to ingest, so they're dropped
rather than carried into empty columns.

Idempotent: re-running deletes and re-inserts this source file's own albums.

The work is `music_service.import_ratings`; this step composes it for the build connection
and prints what it reports.
"""
from client import music_ingest


def run(con, ctx):
    result = music_ingest.compose(con, ctx.inputs, ctx.locations).import_ratings()
    for line in result.lines:
        print(line)
