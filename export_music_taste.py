#!/usr/bin/env python3
"""Export the full music-taste dataset from knowledge.db as one JSON file:
every rated album, per-artist rollups (albums rated + concerts seen), and
concert history. Meant to be handed off whole to another tool/model, not
read as a report -- see export_music_taste_summary() in an earlier version
of this script if a human-readable digest is what's wanted instead.

Read-only, not part of the build pipeline (it queries knowledge.db, it
doesn't build it) -- run anytime after 08_ingest_music_ratings.py has
populated `albums`.

Usage:
    python3 export_music_taste.py                    # JSON to stdout
    python3 export_music_taste.py -o taste.json       # JSON to a file
"""
import sqlite3, os, sys, json, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "knowledge.db")


def rows_as_dicts(cur, sql):
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_export(con):
    cur = con.cursor()

    albums = rows_as_dicts(
        cur,
        "SELECT v.artist, v.title, v.release_year, v.rating, al.rym_id "
        "FROM v_albums v JOIN albums al ON al.id = v.id "
        "ORDER BY v.artist, v.release_year",
    )

    concerts = rows_as_dicts(
        cur,
        "SELECT artist, start_date, end_date, venue, city_state, festival, billing, supporting_for "
        "FROM v_concert_attendances ORDER BY start_date",
    )

    # Counts direct credits AND albums credited to a group this artist is a
    # member of (e.g. Madlib picks up "Freddie Gibbs & Madlib" albums too),
    # via v_albums_with_member_credits -- see schema.sql on artist_members.
    artists = rows_as_dicts(
        cur,
        """
        SELECT
            a.name AS artist,
            COUNT(DISTINCT v.id) AS albums_rated,
            ROUND(AVG(v.rating), 2) AS avg_album_rating,
            COUNT(DISTINCT ca.id) AS concerts_seen
        FROM artists a
        LEFT JOIN v_albums_with_member_credits v ON v.artist = a.name
        LEFT JOIN concert_attendances ca ON ca.artist_id = a.id
        GROUP BY a.id
        HAVING albums_rated > 0 OR concerts_seen > 0
        ORDER BY albums_rated DESC, concerts_seen DESC
        """,
    )

    total, avg_rating = cur.execute("SELECT COUNT(*), ROUND(AVG(rating), 2) FROM albums").fetchone()

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", help="write to this file instead of stdout")
    args = parser.parse_args()

    con = sqlite3.connect(DB)
    data = build_export(con)
    con.close()

    output = json.dumps(data, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
