"""SQLite adapter for the build's `MusicRepository` (`build_repositories_port`): the music tables of the build
database it is given. It decides nothing: it stores the records it is handed and answers for what is stored. Which
artist a name means, which rows a rerun replaces and when to commit are the service's (`music_ingest_service`,
`identity_service`); get-or-create and INSERT OR IGNORE are the port's documented operations."""
import sqlite3
from typing import Iterable, Optional, Sequence, Tuple

import build_records as r


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

    def import_source(self, path: Optional[str]) -> r.ImportSourceId:
        return r.ImportSourceId(self._get_or_create("import_sources", path=path))

    def delete_attendances(self, import_source_id: r.ImportSourceId) -> None:
        self._cur.execute("DELETE FROM concert_attendances WHERE import_source_id = ?", (import_source_id,))

    def delete_albums(self, import_source_id: r.ImportSourceId) -> None:
        self._cur.execute("DELETE FROM albums WHERE import_source_id = ?", (import_source_id,))

    def delete_scrobbles(self, import_source_id: r.ImportSourceId) -> None:
        self._cur.execute("DELETE FROM scrobbles WHERE import_source_id = ?", (import_source_id,))

    def artists(self) -> Iterable[Tuple[r.ArtistId, str]]:
        return self._cur.execute("SELECT id, name FROM artists").fetchall()

    def add_artist(self, name: str) -> r.ArtistId:
        self._cur.execute("INSERT INTO artists (name) VALUES (?)", (name,))
        return r.ArtistId(int(self._cur.lastrowid or 0))

    def venue(self, name: str, city_state: Optional[str]) -> r.VenueId:
        return r.VenueId(self._get_or_create("venues", name=name, city_state=city_state))

    def festival(self, name: str) -> r.FestivalId:
        return r.FestivalId(self._get_or_create("festivals", name=name))

    def add_attendance(self, attendance: r.NewAttendance) -> None:
        self._cur.execute(
            """INSERT INTO concert_attendances (artist_id, start_date, end_date, venue_id, festival_id,
                                                  billing, supporting_for_artist_id, notes, domain, import_source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (attendance.artist_id, attendance.start_date, attendance.end_date, attendance.venue_id,
             attendance.festival_id, attendance.billing, attendance.supporting_for_artist_id, attendance.notes,
             attendance.domain, attendance.import_source_id))

    def add_album(self, album: r.NewAlbum) -> None:
        self._cur.execute(
            """INSERT INTO albums (artist_id, title, release_year, rating, rym_id, import_source_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (album.artist_id, album.title, album.release_year, album.rating, album.rym_id, album.import_source_id))

    def tracks(self) -> Iterable[Tuple[r.TrackId, r.ArtistId, str, Optional[str]]]:
        return self._cur.execute("SELECT id, artist_id, title, album FROM tracks").fetchall()

    def add_track(self, artist_id: r.ArtistId, title: str, album: Optional[str]) -> r.TrackId:
        self._cur.execute("INSERT INTO tracks (artist_id, title, album) VALUES (?, ?, ?)", (artist_id, title, album))
        return r.TrackId(int(self._cur.lastrowid or 0))

    def add_scrobbles(self, scrobbles: Sequence[r.NewScrobble]) -> None:
        self._cur.executemany(
            "INSERT OR IGNORE INTO scrobbles (track_id, played_at, import_source_id) VALUES (?, ?, ?)",
            [(s.track_id, s.played_at, s.import_source_id) for s in scrobbles])

    def add_artist_member(self, group_id: r.ArtistId, member_id: r.ArtistId) -> bool:
        self._cur.execute("INSERT OR IGNORE INTO artist_members (artist_id, member_id) VALUES (?, ?)",
                          (group_id, member_id))
        return self._cur.rowcount > 0

    def totals(self) -> r.MusicTotals:
        row = self._cur.execute(
            """SELECT (SELECT COUNT(*) FROM artists), (SELECT COUNT(*) FROM venues),
                      (SELECT COUNT(*) FROM festivals), (SELECT COUNT(*) FROM tracks),
                      (SELECT COUNT(*) FROM scrobbles)""").fetchone()
        return r.MusicTotals(*row)
