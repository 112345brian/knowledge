"""Ingest the concerts export CSV (see paths.py -> CONCERTS_CSV) into the normalized
artists / venues / festivals / concert_attendances tables. Concert-going is
neither a `facts` claim (no trust/provenance dimension) nor a `measurements`
metric (not a scalar) -- its own shape. And within that shape, artist/venue/
festival are entities worth their own tables, not text repeated on every row:
that's what makes "how many times have I seen X" or "every act at festival Y"
a join instead of a string match.

Parses the source's free-text Location/Notes into structured fields (the rules are in
music_ingest_rules). Idempotent: re-running deletes and re-inserts this source file's own
attendances (and any artists/venues/festivals become orphaned only if nothing else references
them, which is fine -- they're just not re-created if already present).

The work is `music_service.import_concerts`; this step composes it for the build connection
and prints what it reports.
"""
from client import music_ingest


def run(con, ctx):
    result = music_ingest.compose(con, ctx.inputs, ctx.locations).import_concerts()
    for line in result.lines:
        print(line)
