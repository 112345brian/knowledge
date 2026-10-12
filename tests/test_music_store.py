"""The SQLite music adapter against the real client schema: the statements the steps used to run themselves."""
import sqlite3

import pytest

from client import music_store
from schema_helper import full_schema


@pytest.fixture
def repo():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON;")
    con.executescript(full_schema(("concerts", "ratings", "scrobbles")))
    return music_store.SqliteMusicRepository(con)


def rows(repo, sql):
    return repo._con.execute(sql).fetchall()


def test_import_source_is_get_or_create(repo):
    assert repo.import_source("/a.csv") == repo.import_source("/a.csv") != repo.import_source("/b.csv")


def test_venue_and_festival_are_get_or_create_with_null_city_matching(repo):
    assert repo.venue("Hall", None) == repo.venue("Hall", None)
    assert repo.venue("Hall", None) != repo.venue("Hall", "Town, ST")
    assert repo.festival("F") == repo.festival("F")
    assert len(rows(repo, "SELECT * FROM venues")) == 2


def test_artists_are_listed_and_added_in_id_order(repo):
    a, b = repo.add_artist("A"), repo.add_artist("B")
    assert list(repo.artists()) == [(a, "A"), (b, "B")] and a < b
    assert repo.count("artists") == 2


def test_attendance_album_and_forget_touch_only_their_own_source(repo):
    mine, other = repo.import_source("/mine"), repo.import_source("/other")
    artist = repo.add_artist("A")
    for src in (mine, other):
        repo.add_attendance(artist, "2020-01-01", None, None, None, "headliner", None, "n", src)
        repo.add_album(artist, f"T{src}", 2001, 8, f"R{src}", src)
    repo.forget_attendances(mine)
    repo.forget_albums(mine)
    assert rows(repo, "SELECT import_source_id, domain FROM concert_attendances") == [(other, "music")]
    assert rows(repo, "SELECT import_source_id FROM albums") == [(other,)]


def test_tracks_scrobbles_ignore_duplicates_and_forget_by_source(repo):
    src, other = repo.import_source("/s"), repo.import_source("/o")
    artist = repo.add_artist("A")
    track = repo.add_track(artist, "Song", "Album")
    assert list(repo.tracks()) == [(track, artist, "Song", "Album")]
    repo.add_scrobbles([(track, "2020-01-01T00:00:00Z", src), (track, "2020-01-01T00:00:00Z", src),
                        (track, "2020-01-02T00:00:00Z", other)])
    assert repo.count("scrobbles") == 2 and repo.count("tracks") == 1
    repo.forget_scrobbles(src)
    assert repo.count("scrobbles") == 1


def test_add_member_reports_whether_a_row_was_new(repo):
    g, m = repo.add_artist("G"), repo.add_artist("M")
    assert repo.add_member(g, m) is True
    assert repo.add_member(g, m) is False


def test_count_only_knows_the_music_tables(repo):
    with pytest.raises(KeyError):
        repo.count("facts; DROP TABLE artists")


def test_commit_makes_rows_visible_to_another_connection(tmp_path):
    path = str(tmp_path / "m.db")
    con = sqlite3.connect(path)
    con.executescript(full_schema(("concerts",)))
    repo = music_store.SqliteMusicRepository(con)
    repo.add_artist("A")
    repo.commit()
    assert sqlite3.connect(path).execute("SELECT name FROM artists").fetchall() == [("A",)]
