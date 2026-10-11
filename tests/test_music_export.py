"""The music-taste export: the document's shape, the SQL adapter behind its read port, and the tool's output
(stdout vs file, messages) through the injected reads and DataFiles."""
import io
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import local_datafiles  # noqa: E402
import music_export_store  # noqa: E402
from client import export_music_taste  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "knowledge.db")
    con = sqlite3.connect(path)
    con.executescript(open(os.path.join(REPO, "client", "music.sql")).read())
    con.executescript("""
        INSERT INTO artists(id, name) VALUES (1,'Alpha'),(2,'Beta'),(3,'Gamma'),(4,'Quiet');
        INSERT INTO venues(id, name, city_state) VALUES (1,'The Hall','Austin, TX');
        INSERT INTO festivals(id, name) VALUES (1,'Fest');
        INSERT INTO concert_attendances(id, artist_id, start_date, end_date, venue_id, festival_id, billing, supporting_for_artist_id)
            VALUES (1,1,'2024-05-01',NULL,1,NULL,'headliner',NULL),
                   (2,2,'2023-01-02','2023-01-03',NULL,1,'festival-set',NULL),
                   (3,3,'2023-01-02',NULL,1,NULL,'opener',1);
        INSERT INTO albums(id, artist_id, title, release_year, rating, rym_id)
            VALUES (1,1,'First',2001,8,'A1'),(2,1,'Second',1999,6,'A2'),(3,2,'Duo',2010,9,'B1');
        INSERT INTO artist_members(artist_id, member_id) VALUES (2,1);
    """)
    con.commit()
    con.close()
    return path


def test_export_document_shape_and_values(db):
    reads = music_export_store.open_reads(db)
    try:
        data = export_music_taste.build_export(reads)
    finally:
        reads.close()
    assert list(data) == ["summary", "artists", "albums", "concerts"]
    assert data["summary"] == {"albums_rated": 3, "average_rating": 7.67, "distinct_artists": 3, "concerts_attended": 3}
    assert data["albums"] == [
        {"artist": "Alpha", "title": "Second", "release_year": 1999, "rating": 6, "rym_id": "A2"},
        {"artist": "Alpha", "title": "First", "release_year": 2001, "rating": 8, "rym_id": "A1"},
        {"artist": "Beta", "title": "Duo", "release_year": 2010, "rating": 9, "rym_id": "B1"},
    ]
    assert [c["artist"] for c in data["concerts"]] == ["Beta", "Gamma", "Alpha"]      # by start_date
    assert list(data["concerts"][0]) == ["artist", "start_date", "end_date", "venue", "city_state", "festival",
                                         "billing", "supporting_for"]
    assert data["concerts"][1]["supporting_for"] == "Alpha" and data["concerts"][1]["venue"] == "The Hall"
    # Alpha: 2 direct albums + the Beta/Alpha collaboration credited through membership, 1 concert
    assert data["artists"] == [
        {"artist": "Alpha", "albums_rated": 3, "avg_album_rating": 7.67, "concerts_seen": 1},
        {"artist": "Beta", "albums_rated": 1, "avg_album_rating": 9.0, "concerts_seen": 1},
        {"artist": "Gamma", "albums_rated": 0, "avg_album_rating": None, "concerts_seen": 1},
    ]


def test_an_empty_database_exports_an_empty_document(tmp_path):
    path = str(tmp_path / "e.db")
    con = sqlite3.connect(path)
    con.executescript(open(os.path.join(REPO, "client", "music.sql")).read())
    con.close()
    reads = music_export_store.open_reads(path)
    data = export_music_taste.build_export(reads)
    reads.close()
    assert data == {"summary": {"albums_rated": 0, "average_rating": None, "distinct_artists": 0,
                                "concerts_attended": 0}, "artists": [], "albums": [], "concerts": []}


def test_run_prints_json_to_stdout_or_writes_the_file(db, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert export_music_taste.run(music_export_store.open_reads(db), local_datafiles, None, out, err) == 0
    document = json.loads(out.getvalue())
    assert err.getvalue() == "" and out.getvalue().endswith("}\n")
    target = str(tmp_path / "taste.json")
    out, err = io.StringIO(), io.StringIO()
    assert export_music_taste.run(music_export_store.open_reads(db), local_datafiles, target, out, err) == 0
    assert out.getvalue() == "" and err.getvalue() == f"Wrote {target}\n"
    assert open(target, encoding="utf-8").read() == json.dumps(document, indent=2, ensure_ascii=False)


def test_the_store_satisfies_its_port_and_the_export_module_holds_no_sql():
    import music_export_port
    for member in ("albums", "concerts", "artists", "album_totals", "close"):
        assert callable(getattr(music_export_store.MusicExportStore, member)), member
    assert hasattr(music_export_port, "MusicExportReads")
    text = open(os.path.join(REPO, "client", "export_music_taste.py")).read()
    assert not any(w in text for w in ("SELECT ", "sqlite3", "cursor", ".execute("))
