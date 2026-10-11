"""SQLite adapter for `music_repository_port.MusicRepository`: the music tables of the build database it is
given. Every statement of SQL the client music steps used lives here; the services only see ids and rows."""
import sqlite3
from typing import Iterable, Optional, Sequence, Tuple

COUNTED = {"artists": "artists", "venues": "venues", "festivals": "festivals", "scrobbles": "scrobbles",
           "tracks": "tracks"}


class SqliteMusicRepository:
    def __init__(self, con: sqlite3.Connection):
        self._con = con
        self._cur = con.cursor()

    def _get_or_create(self, table: str, **fields) -> int:
        where = " AND ".join(f"{k} IS :{k}" for k in fields)
        row = self._cur.execute(f"SELECT id FROM {table} WHERE {where}", fields).fetchone()
        if row:
            return row[0]
        cols = ", ".join(fields)
        placeholders = ", ".join(f":{k}" for k in fields)
        self._cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", fields)
        return int(self._cur.lastrowid or 0)

    def import_source(self, path: str) -> int:
        return self._get_or_create("import_sources", path=path)

    def forget_attendances(self, import_source_id: int) -> None:
        self._cur.execute("DELETE FROM concert_attendances WHERE import_source_id = ?", (import_source_id,))

    def forget_albums(self, import_source_id: int) -> None:
        self._cur.execute("DELETE FROM albums WHERE import_source_id = ?", (import_source_id,))

    def forget_scrobbles(self, import_source_id: int) -> None:
        self._cur.execute("DELETE FROM scrobbles WHERE import_source_id = ?", (import_source_id,))

    def artists(self) -> Iterable[Tuple[int, str]]:
        return self._cur.execute("SELECT id, name FROM artists").fetchall()

    def add_artist(self, name: str) -> int:
        self._cur.execute("INSERT INTO artists (name) VALUES (?)", (name,))
        return int(self._cur.lastrowid or 0)

    def venue(self, name: str, city_state: Optional[str]) -> int:
        return self._get_or_create("venues", name=name, city_state=city_state)

    def festival(self, name: str) -> int:
        return self._get_or_create("festivals", name=name)

    def add_attendance(self, artist_id: int, start_date: str, end_date: Optional[str], venue_id: Optional[int],
                       festival_id: Optional[int], billing: str, supporting_artist_id: Optional[int],
                       notes: Optional[str], import_source_id: int) -> None:
        self._cur.execute(
            """INSERT INTO concert_attendances (artist_id, start_date, end_date, venue_id, festival_id,
                                                  billing, supporting_for_artist_id, notes, domain, import_source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'music', ?)""",
            (artist_id, start_date, end_date, venue_id, festival_id, billing, supporting_artist_id, notes,
             import_source_id))

    def add_album(self, artist_id: int, title: str, release_year: int, rating: int, rym_id: str,
                  import_source_id: int) -> None:
        self._cur.execute(
            """INSERT INTO albums (artist_id, title, release_year, rating, rym_id, import_source_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (artist_id, title, release_year, rating, rym_id, import_source_id))

    def tracks(self) -> Iterable[Tuple[int, int, str, str]]:
        return self._cur.execute("SELECT id, artist_id, title, album FROM tracks").fetchall()

    def add_track(self, artist_id: int, title: str, album: str) -> int:
        self._cur.execute("INSERT INTO tracks (artist_id, title, album) VALUES (?, ?, ?)", (artist_id, title, album))
        return int(self._cur.lastrowid or 0)

    def add_scrobbles(self, rows: Sequence[Tuple[int, str, int]]) -> None:
        self._cur.executemany(
            "INSERT OR IGNORE INTO scrobbles (track_id, played_at, import_source_id) VALUES (?, ?, ?)", rows)

    def add_member(self, artist_id: int, member_id: int) -> bool:
        self._cur.execute("INSERT OR IGNORE INTO artist_members (artist_id, member_id) VALUES (?, ?)",
                          (artist_id, member_id))
        return self._cur.rowcount > 0

    def count(self, what: str) -> int:
        return self._cur.execute(f"SELECT COUNT(*) FROM {COUNTED[what]}").fetchone()[0]

    def commit(self) -> None:
        self._con.commit()
