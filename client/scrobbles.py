"""Ingest a Last.fm scrobble export into `tracks`/`scrobbles`, reusing
`artists` (shared with concert_attendances/albums) for artist identity.

The export format is a list of *pages* (one per API call during the original
scrape), each page a list of track dicts -- not a flat list of scrobbles
(music_ingest_rules.scrobbles_from_pages flattens it). Repeated text isn't stored
per play -- 153k scrobbles collapse to ~30k distinct tracks (avg 5
plays/track), so each gets one `tracks` row; and the source file path
(previously repeated as ~80 bytes of text on all 153k rows, 12MB+ of pure
duplication) gets one `import_sources` row instead. Same get-or-create /
delete-by-source idempotency pattern as concerts.py and music_ratings.py otherwise.

Scrobble exports may represent group or collaborative credits as one artist
string. There's no safe general rule for splitting those credits, so each
string becomes its own artist row. Membership facts are curated separately
in artist_members (seed_artist_members), not derived here.

The work is `music_service.import_scrobbles`; this step composes it for the build connection
and prints what it reports.
"""
from client import music_ingest


def run(con, ctx):
    result = music_ingest.compose(con, ctx.inputs, ctx.locations).import_scrobbles()
    for line in result.lines:
        print(line)
