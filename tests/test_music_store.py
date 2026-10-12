"""The SQLite music adapter against the real client schema: the statements the steps used to run themselves."""
import sqlite3

import pytest

from client import music_records as r
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
    assert repo.totals().artists == 2


def test_attendance_album_and_delete_touch_only_their_own_source(repo):
    mine, other = repo.import_source("/mine"), repo.import_source("/other")
    artist = repo.add_artist("A")
    for src in (mine, other):
        repo.add_attendance(r.NewAttendance(artist, "2020-01-01", None, None, None, "headliner", None, "n", src))
        repo.add_album(r.NewAlbum(artist, f"T{src}", 2001, 8, f"R{src}", src))
    repo.delete_attendances(mine)
    repo.delete_albums(mine)
    assert rows(repo, "SELECT import_source_id, domain FROM concert_attendances") == [(other, "music")]
    assert rows(repo, "SELECT import_source_id FROM albums") == [(other,)]


def test_tracks_scrobbles_ignore_duplicates_and_delete_by_source(repo):
    src, other = repo.import_source("/s"), repo.import_source("/o")
    artist = repo.add_artist("A")
    track = repo.add_track(artist, "Song", "Album")
    assert list(repo.tracks()) == [(track, artist, "Song", "Album")]
    repo.add_scrobbles([r.NewScrobble(track, "2020-01-01T00:00:00Z", src),
                        r.NewScrobble(track, "2020-01-01T00:00:00Z", src),
                        r.NewScrobble(track, "2020-01-02T00:00:00Z", other)])
    assert repo.totals().scrobbles == 2 and repo.totals().tracks == 1
    repo.delete_scrobbles(src)
    assert repo.totals().scrobbles == 1


def test_add_artist_member_reports_whether_a_row_was_new(repo):
    g, m = repo.add_artist("G"), repo.add_artist("M")
    assert repo.add_artist_member(g, m) is True
    assert repo.add_artist_member(g, m) is False


def test_totals_count_the_music_tables(repo):
    repo.add_artist("A")
    repo.venue("V", None)
    repo.festival("F")
    assert repo.totals() == r.MusicTotals(artists=1, venues=1, festivals=1, tracks=0, scrobbles=0)


def test_the_adapter_decides_nothing_about_who_an_artist_is(repo):
    """Case folding, HTML unescaping and aliases are the directory's: a name the service hands over is stored as it
    is. Only the schema's own unique index on SQLite's ASCII-only lower(name) refuses a repeat; a non-ASCII case
    variant is a second row here, which is why the directory folds with Python's lower() before it ever asks."""
    first, second = repo.add_artist("Ünter"), repo.add_artist("ünter")
    assert first != second
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_artist("ÜNTER")  # lower() in SQLite folds ASCII only: same key as "Ünter"
    repo.add_artist("A &amp; B")
    assert [n for _, n in repo.artists()] == ["Ünter", "ünter", "A &amp; B"]


def test_the_adapter_never_commits(tmp_path):
    """Committing is the unit of work's: a row is invisible to another connection until the service commits."""
    path = str(tmp_path / "m.db")
    con = sqlite3.connect(path)
    con.executescript(full_schema(("concerts",)))
    con.commit()
    repo = music_store.SqliteMusicRepository(con)
    repo.add_artist("A")
    repo.totals()
    assert sqlite3.connect(path).execute("SELECT name FROM artists").fetchall() == []
    con.commit()
    assert sqlite3.connect(path).execute("SELECT name FROM artists").fetchall() == [("A",)]
