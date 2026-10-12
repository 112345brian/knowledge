"""The four music steps end to end over the real client schema (`client.music_ingest.compose` -> service -> SQLite adapter).

These pin what the callers of the music repository see: the rows, their ids in first-seen order, what a rerun
replaces, what is committed when, and the edge inputs (blank, NUL, partial objects, a failure on the second
attempt). They go through the composition root only, so they hold across a change of the port beneath it.
"""
import ast
import glob
import json
import os
import sqlite3

import pytest

from client import music_ingest
from locations import Locations
from music_fakes import FakeInputs
from schema_helper import full_schema

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = "/data"
LOCS = Locations(knowledge_db_dir="/db", private_data_dir=DATA, concerts_csv="/in/concerts.csv",
                 rym_export_csv="/in/rym.csv", scrobbles_json="/in/scrobbles.json")
RULES_PATH = f"{DATA}/artist_rules.json"
CSV_HEADER = "Concert,Start Date,End Date,Location,Notes\n"
RYM_HEADER = " First Name,Last Name,First Name localized, Last Name localized,Title,Release_Date,Rating,RYM Album\n"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "music.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON;")
    con.executescript(full_schema(("concerts", "ratings", "scrobbles")))
    con.commit()
    yield con, path
    con.close()


def compose(con, files=None, rules=None):
    files = dict(files or {})
    if rules is not None:
        files[RULES_PATH] = json.dumps({"version": 1, **rules})
    return music_ingest.compose(con, FakeInputs(files), LOCS)


def table(con, sql):
    return con.execute(sql).fetchall()


def page(*tracks):
    return [{"artist": {"#text": a}, "name": n, "album": {"#text": al}, "date": {"uts": str(u)}}
            for a, n, al, u in tracks]


# ---------------------------------------------------------------------------------------------- concerts

def test_concerts_rows_and_ids_follow_first_seen_order(db):
    con, _ = db
    csv_text = (CSV_HEADER
                + "Phoebe Bridgers,2019-06-14,,Bonnaroo,Festival set\n"
                + "Boygenius,2023-05-01,,\"The Fillmore, San Francisco, CA\",opened for Big Thief; great\n"
                + "phoebe bridgers,2024-01-01,,The Fillmore,\n")
    result = compose(con, {LOCS.concerts_csv: csv_text}, {"aliases": {}, "members": {}, "festivals": ["Bonnaroo"]}) \
        .import_concerts()
    assert table(con, "SELECT id, name FROM artists ORDER BY id") == [(1, "Phoebe Bridgers"), (2, "Boygenius"),
                                                                       (3, "Big Thief")]
    assert table(con, "SELECT id, name, city_state FROM venues ORDER BY id") == [
        (1, "The Fillmore", "San Francisco, CA"), (2, "The Fillmore", None)]
    assert table(con, "SELECT id, name FROM festivals") == [(1, "Bonnaroo")]
    assert table(con, "SELECT artist_id, start_date, end_date, venue_id, festival_id, billing, "
                      "supporting_for_artist_id, notes, domain FROM concert_attendances ORDER BY id") == [
        (1, "2019-06-14", None, None, 1, "festival-set", None, "Festival set", "music"),
        (2, "2023-05-01", None, 1, None, "opener", 3, "opened for Big Thief; great", "music"),
        (1, "2024-01-01", None, 2, None, "headliner", None, None, "music")]
    assert result.counts == {"concert_attendances": 3, "artists": 3, "venues": 2, "festivals": 1,
                             "unresolved_festivals": 0}


def test_concerts_blank_or_nameless_rows_are_skipped_and_nul_names_survive(db):
    con, _ = db
    csv_text = (CSV_HEADER + "   ,2020-01-01,,Hall,\n" + "No Date,   ,,Hall,\n" + "A\x00B,2020-01-02,,Hall,\n"
                + "a\x00b,2020-01-03,,Hall,\n")
    result = compose(con, {LOCS.concerts_csv: csv_text}).import_concerts()
    assert table(con, "SELECT name FROM artists") == [("A\x00B",)]
    assert result.counts["concert_attendances"] == 2


def test_concert_names_use_aliases_and_html_unescape_to_one_artist(db):
    con, _ = db
    csv_text = CSV_HEADER + "Simon &amp; Garfunkel,2020-01-01,,Hall,\n" + "S&G,2020-01-02,,Hall,\n"
    compose(con, {LOCS.concerts_csv: csv_text}, {"aliases": {"s&g": "Simon & Garfunkel"}, "members": {}}) \
        .import_concerts()
    assert table(con, "SELECT name FROM artists") == [("Simon & Garfunkel",)]
    assert table(con, "SELECT DISTINCT artist_id FROM concert_attendances") == [(1,)]


def test_concerts_rerun_replaces_only_its_own_source_and_commits(db):
    con, path = db
    files = {LOCS.concerts_csv: CSV_HEADER + "Act,2020-01-01,,Hall,\n"}
    steps = compose(con, files)
    steps.import_concerts()
    steps.import_concerts()
    other = con.execute("INSERT INTO import_sources (path) VALUES ('/elsewhere.csv')").lastrowid
    con.execute("INSERT INTO concert_attendances (artist_id, start_date, billing, domain, import_source_id) "
                "VALUES (1, '2000-01-01', 'headliner', 'music', ?)", (other,))
    con.commit()
    steps.import_concerts()
    assert table(con, "SELECT import_source_id FROM concert_attendances ORDER BY id") == [(other,), (1,)]
    assert table(con, "SELECT COUNT(*) FROM artists") == [(1,)]
    # what a step reports it has also been committed: another connection sees it
    seen = sqlite3.connect(path).execute("SELECT COUNT(*) FROM concert_attendances").fetchone()
    assert seen == (2,)


def test_a_bad_rules_file_fails_before_anything_is_written_or_committed(db):
    con, path = db
    files = {LOCS.concerts_csv: CSV_HEADER + "Act,2020-01-01,,Hall,\n", RULES_PATH: json.dumps({"version": 9})}
    with pytest.raises(RuntimeError, match="expected version 1"):
        compose(con, files).import_concerts()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM concert_attendances").fetchone() == (0,)
    assert table(con, "SELECT COUNT(*) FROM artists") == [(0,)]


# ----------------------------------------------------------------------------------------------- ratings

def test_ratings_share_the_artists_of_the_concerts(db):
    con, _ = db
    steps = compose(con, {LOCS.concerts_csv: CSV_HEADER + "Björk,2020-01-01,,Hall,\n",
                          LOCS.rym_export_csv: RYM_HEADER + "Bj&ouml;rk,,,,Homogenic &amp; More,1997,9,A1\n"
                                               + ",,Sigur,Rós,Ágætis,1999,8,A2\n" + ",,,,No Artist,2000,5,A3\n"})
    steps.import_concerts()
    result = steps.import_ratings()
    assert table(con, "SELECT id, name FROM artists ORDER BY id") == [(1, "Björk"), (2, "Sigur Rós")]
    assert table(con, "SELECT artist_id, title, release_year, rating, rym_id, import_source_id FROM albums "
                      "ORDER BY id") == [(1, "Homogenic & More", 1997, 9, "A1", 2), (2, "Ágætis", 1999, 8, "A2", 2)]
    assert result.counts == {"albums": 2, "artists": 2}
    steps.import_ratings()
    assert table(con, "SELECT COUNT(*) FROM albums") == [(2,)]


# --------------------------------------------------------------------------------------------- scrobbles

def test_scrobbles_tracks_artists_and_dedupe(db):
    con, _ = db
    pages = [page(("Artist A", "Song", "Alb", 1000), ("artist a", "Song", "Alb", 2000),
                  ("Artist A", "Song", "Alb", 1000), ("Artist B", "Other", "", 3000)),
             [{"artist": {"#text": "Artist A"}, "name": "Now Playing", "album": {"#text": ""}},     # no date
              {"artist": {"#text": "Artist A"}, "name": "Partial", "date": {"uts": "4000"}},          # no album key
              {"name": "No Artist", "date": {"uts": "5000"}},
              {"artist": {"#text": "Artist C"}, "name": "No Uts", "date": {}}]]
    result = compose(con, {LOCS.scrobbles_json: json.dumps(pages)}).import_scrobbles()
    assert table(con, "SELECT id, name FROM artists ORDER BY id") == [(1, "Artist A"), (2, "Artist B")]
    assert table(con, "SELECT id, artist_id, title, album FROM tracks ORDER BY id") == [
        (1, 1, "Song", "Alb"), (2, 2, "Other", ""), (3, 1, "Partial", "")]
    assert table(con, "SELECT track_id, played_at FROM scrobbles ORDER BY id") == [
        (1, "1970-01-01T00:16:40Z"), (1, "1970-01-01T00:33:20Z"), (2, "1970-01-01T00:50:00Z"),
        (3, "1970-01-01T01:06:40Z")]
    assert result.counts == {"parsed": 5, "new_artists": 2, "new_tracks": 3, "scrobbles": 4, "tracks": 3,
                             "artists": 2}


def test_a_missing_dump_touches_nothing(db):
    con, _ = db
    result = compose(con).import_scrobbles()
    assert result.skipped and table(con, "SELECT COUNT(*) FROM import_sources") == [(0,)]


def test_a_failure_on_the_second_run_leaves_the_first_runs_committed_rows_for_other_connections(db):
    con, path = db
    good = json.dumps([page(("A", "S", "", 1000))])
    compose(con, {LOCS.scrobbles_json: good}).import_scrobbles()
    with pytest.raises(json.JSONDecodeError):
        compose(con, {LOCS.scrobbles_json: "{not json"}).import_scrobbles()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM scrobbles").fetchone() == (1,)
    # the replacement had begun (the old rows were forgotten) in the failing run's own, uncommitted, transaction
    assert table(con, "SELECT COUNT(*) FROM scrobbles") == [(0,)]


def test_an_unconfigured_input_names_its_setting_and_writes_nothing(db):
    con, _ = db
    bare = Locations(knowledge_db_dir="/db", private_data_dir=DATA)
    steps = music_ingest.compose(con, FakeInputs(), bare)
    for fn, setting in ((steps.import_concerts, "CONCERTS_CSV"), (steps.import_ratings, "RYM_EXPORT_CSV"),
                        (steps.import_scrobbles, "SCROBBLES_JSON")):
        with pytest.raises(ValueError, match=setting):
            fn()
    assert table(con, "SELECT COUNT(*) FROM import_sources") == [(0,)]


# ------------------------------------------------------------------------------------------ artist members

def test_members_create_missing_artists_and_count_only_new_links(db):
    con, _ = db
    steps = compose(con, {LOCS.concerts_csv: CSV_HEADER + "Alice,2020-01-01,,Hall,\n"},
                    {"aliases": {"the duo": "Duo"}, "members": {"The Duo": ["Alice", "bob"], "Empty": []}})
    steps.import_concerts()
    first = steps.seed_artist_members()
    assert first.counts == {"artist_members": 2}
    assert table(con, "SELECT id, name FROM artists ORDER BY id") == [(1, "Alice"), (2, "Duo"), (3, "bob"),
                                                                       (4, "Empty")]
    assert table(con, "SELECT artist_id, member_id FROM artist_members ORDER BY rowid") == [(2, 1), (2, 3)]
    assert steps.seed_artist_members().counts == {"artist_members": 0}


def test_members_without_a_rules_file_insert_nothing(db):
    con, _ = db
    assert compose(con).seed_artist_members().counts == {"artist_members": 0}
    assert table(con, "SELECT COUNT(*) FROM artists") == [(0,)]


# ------------------------------------------------------------------------------------- who may write what

def _calls(attr):
    """(file, line, discarded) of every `.attr(...)` call outside tests and the worktrees."""
    found = []
    for path in glob.glob(os.path.join(REPO, "**", "*.py"), recursive=True):
        rel = os.path.relpath(path, REPO).replace(os.sep, "/")
        if rel.startswith((".venv/", ".claude/", "tests/")):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        discarded = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == attr:
                found.append((rel, node.lineno, id(node) in discarded))
    return found


def test_only_the_artist_directory_creates_artists():
    """Artist identity (the key, the canonical spelling, first-seen ids) is decided in one place."""
    assert sorted({f for f, _, _ in _calls("add_artist")}) == ["client/artist_directory.py"]


def test_nobody_discards_whether_a_membership_was_new():
    calls = _calls("add_artist_member") + _calls("add_member")
    assert calls and not [c for c in calls if c[2]], calls
