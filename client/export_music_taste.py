#!/usr/bin/env python3
"""Export the full music-taste dataset from knowledge.db as one JSON file:
every rated album, per-artist rollups (albums rated + concerts seen), and
concert history. Meant to be handed off whole to another tool/model, not
read as a report -- see export_music_taste_summary() in an earlier version
of this script if a human-readable digest is what's wanted instead.

Read-only, not part of the build pipeline (it queries knowledge.db, it
doesn't build it) -- run anytime after music_ratings.py has
populated `albums`.

Usage:
    python3 -m client.export_music_taste                    # JSON to stdout
    python3 -m client.export_music_taste -o taste.json       # JSON to a file
"""
import argparse
import json
import sys


def build_export(reads):
    """The export document from a `MusicExportReads` (records out; no SQL, no connection here)."""
    albums = reads.albums()
    concerts = reads.concerts()
    artists = reads.artists()
    total, avg_rating = reads.album_totals()

    return {
        "summary": {
            "albums_rated": total,
            "average_rating": avg_rating,
            "distinct_artists": len(artists),
            "concerts_attended": len(concerts),
        },
        "artists": artists,
        "albums": albums,
        "concerts": concerts,
    }


def run(reads, files, output=None, stdout=None, stderr=None):
    """Build the export and write it to the file `output`, or to `stdout`. Returns the exit code (0).
    `reads` is a MusicExportReads, `files` a DataFiles; the reads are closed afterwards."""
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    try:
        data = build_export(reads)
    finally:
        reads.close()
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if output:
        files.write_text(output, text)
        print(f"Wrote {output}", file=stderr)
    else:
        print(text, file=stdout)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", help="write to this file instead of stdout")
    args = parser.parse_args(argv)

    from client import music_export_store
    from container import Container
    app = Container()
    return run(music_export_store.open_reads(app.locations.db_path), app.datafiles, args.output)


if __name__ == "__main__":
    sys.exit(main())
