"""Adapter for `music_export_port.MusicExportReads`: the SQL behind the music-taste export, run over the
knowledge database at `db_path`. The connection is opened on first use and released by `close()`."""
import sqlite3

ALBUMS_SQL = (
    "SELECT v.artist, v.title, v.release_year, v.rating, al.rym_id "
    "FROM v_albums v JOIN albums al ON al.id = v.id "
    "ORDER BY v.artist, v.release_year"
)

CONCERTS_SQL = (
    "SELECT artist, start_date, end_date, venue, city_state, festival, billing, supporting_for "
    "FROM v_concert_attendances ORDER BY start_date"
)

# Counts direct credits AND albums credited to a group this artist is a member of (so collaborative albums can
# be included too), via v_albums_with_member_credits -- see schema.sql on artist_members.
ARTISTS_SQL = """
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
        """

TOTALS_SQL = "SELECT COUNT(*), ROUND(AVG(rating), 2) FROM albums"


class MusicExportStore:
    def __init__(self, db_path):
        self._db_path = db_path
        self._con = None

    def _connection(self):
        if self._con is None:
            self._con = sqlite3.connect(self._db_path)
        return self._con

    def _records(self, sql):
        cur = self._connection().execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def albums(self):
        return self._records(ALBUMS_SQL)

    def concerts(self):
        return self._records(CONCERTS_SQL)

    def artists(self):
        return self._records(ARTISTS_SQL)

    def album_totals(self):
        total, avg_rating = self._connection().execute(TOTALS_SQL).fetchone()
        return total, avg_rating

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None


def open_reads(db_path):
    """The reads for the database at `db_path` (nothing is opened until the first read)."""
    return MusicExportStore(db_path)
