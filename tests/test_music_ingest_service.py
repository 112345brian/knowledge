"""The music client steps as application services, against a fake repository and a fake Inputs: canonical artist
identity, the per-run directory, replacement of a source's own rows, festival handling and the reported counts."""
import json
import os

import pytest

from client.artist_rules import ArtistRules, parse_artist_rules
from locations import Locations
from music_fakes import FakeInputs, FakeMusicRepository
import music_ingest_service as svc

CSV_HEADER = "Concert,Start Date,End Date,Location,Notes\n"
DATA = "/data"
LOCS = Locations(knowledge_db_dir="/db", private_data_dir=DATA, concerts_csv="/in/concerts.csv",
                 rym_export_csv="/in/rym.csv", scrobbles_json="/in/scrobbles.json")
RULES_PATH = os.path.join(DATA, "artist_rules.json")


def deps(repo, files=None, rules=None):
    files = dict(files or {})
    if rules is not None:
        files[RULES_PATH] = json.dumps({"version": 1, **rules})
    return svc.MusicIngestDeps(music=repo, inputs=FakeInputs(files), locations=LOCS)


def rym_row(first, last, title, year=2001, rating=8, rym="Album1"):
    return f"{first},{last},,,{title},{year},{rating},{rym}\n"


# album_artist_name reads ' First Name' (leading space), 'Last Name', 'First Name localized' and ' Last Name localized'
RYM_HEADER = " First Name,Last Name,First Name localized, Last Name localized,Title,Release_Date,Rating,RYM Album\n"


# ---------------------------------------------------------------- rules and directory
def test_no_rules_file_means_no_rules():
    assert svc.load_artist_rules(FakeInputs(), DATA) == ArtistRules()


def test_rules_file_must_be_version_1_with_aliases_and_members():
    for bad in ({"version": 2, "aliases": {}, "members": {}}, {"version": 1, "aliases": [], "members": {}},
                {"version": 1, "aliases": {}}, {"version": 1, "aliases": {}, "members": {}, "festivals": "x"}):
        with pytest.raises(RuntimeError, match="expected version 1 with aliases and members"):
            svc.load_artist_rules(FakeInputs({RULES_PATH: json.dumps(bad)}), DATA)
    with pytest.raises(RuntimeError, match="cannot read artist rules"):
        svc.load_artist_rules(FakeInputs({RULES_PATH: "{not json"}), DATA)


def test_parse_artist_rules_keeps_aliases_members_and_festivals():
    r = parse_artist_rules({"version": 1, "aliases": {"a": "A"}, "members": {"G": ["m"]}, "festivals": ["F"]}, "p")
    assert (r.aliases, r.members, r.festivals) == ({"a": "A"}, {"G": ["m"]}, ["F"])


def test_directory_resolves_case_insensitively_unescapes_and_applies_aliases():
    repo = FakeMusicRepository(artists=[(5, "JPEGMAFIA")])
    d = svc.ArtistDirectory.load(repo, ArtistRules(aliases={"the band": "Band, The"}))
    assert d.resolve("Jpegmafia") == 5                      # case-folded onto the existing row
    assert d.resolve("Simon &amp; Garfunkel") == d.resolve("simon & garfunkel")
    assert d.resolve("THE BAND") == d.resolve("band, the")  # alias, then folded
    assert repo.artist_names() == ["JPEGMAFIA", "Simon & Garfunkel", "Band, The"]
    assert len(d) == 3


def test_first_existing_row_wins_when_two_differ_only_by_case():
    repo = FakeMusicRepository(artists=[(1, "Foo"), (2, "FOO")])
    assert svc.ArtistDirectory.load(repo, ArtistRules()).resolve("foo") == 1


def test_a_directory_lives_one_run_and_is_never_shared():
    repo = FakeMusicRepository()
    first = svc.ArtistDirectory.load(repo, ArtistRules())
    first.resolve("New Act")
    second = svc.ArtistDirectory.load(repo, ArtistRules())      # a later run sees it through the repository, not the object
    assert len(first) == len(second) == 1
    second.resolve("Another")
    assert len(first) == 1


# ---------------------------------------------------------------- concerts
def test_concerts_festival_opener_billing_and_counts():
    csv_text = (CSV_HEADER
                + "Bonnaroo,2019-06-13,2019-06-16,Manchester TN,\n"          # a known festival named as the concert
                + "Phoebe Bridgers,2019-06-14,,Bonnaroo,Festival set\n"      # location is the festival
                + "Boygenius,2023-05-01,,\"The Fillmore, San Francisco, CA\",opened for Big Thief; great\n"
                + "Some Band,2022-01-01,,Dive Bar,Music Festival promo\n"    # festival in notes, none resolvable
                + ",2022-01-01,,Nowhere,\n"                                  # no name -> skipped
                + "No Date,,,Nowhere,\n")                                    # no start date -> skipped
    repo = FakeMusicRepository()
    result = svc.import_concerts(deps(repo, {LOCS.concerts_csv: csv_text}, {"aliases": {}, "members": {},
                                                                              "festivals": ["Bonnaroo"]}))
    assert result.step == "concerts" and not result.skipped
    assert result.counts == {"concert_attendances": 4, "artists": 5, "venues": 3, "festivals": 1,
                             "unresolved_festivals": 1}
    assert result.lines == (
        "[concerts] inserted 4 concert_attendances",
        "  artists: 5, venues: 3, festivals: 1",
        "  festival rows with no resolvable festival name: 1 (Location held a bare city or similar -- not guessed)",
        "  billing=festival-set: 3",
        "  billing=opener: 1")
    assert repo.artist_names() == ["Bonnaroo", "Phoebe Bridgers", "Boygenius", "Big Thief", "Some Band"]
    bonnaroo, phoebe, boygenius, some = repo.attendances
    assert bonnaroo["festival_id"] == phoebe["festival_id"] == 1
    assert boygenius["billing"] == "opener" and boygenius["supporting"] == 4 and boygenius["notes"] == "opened for Big Thief; great"
    assert some["billing"] == "festival-set" and some["festival_id"] is None   # festival named only in the notes
    assert repo.commits == 1


def test_a_rerun_replaces_only_this_sources_attendances():
    repo = FakeMusicRepository()
    other = repo.import_source("/elsewhere.csv")
    repo.add_artist("Old")
    repo.add_attendance(1, "2000-01-01", None, None, None, "headliner", None, None, other)
    d = deps(repo, {LOCS.concerts_csv: CSV_HEADER + "Act,2020-01-01,,Hall,\n"})
    svc.import_concerts(d)
    svc.import_concerts(d)
    mine = repo.sources[LOCS.concerts_csv]
    assert [a["import_source_id"] for a in repo.attendances].count(mine) == 1     # not doubled
    assert [a["import_source_id"] for a in repo.attendances].count(other) == 1    # untouched
    assert repo.artist_names() == ["Old", "Act"]                                  # not re-created


def test_concert_names_resolve_through_canonical_artist_identity():
    repo = FakeMusicRepository(artists=[(1, "Jpegmafia")])
    svc.import_concerts(deps(repo, {LOCS.concerts_csv: CSV_HEADER + "JPEGMAFIA,2020-01-01,,Hall,\n"}))
    assert repo.attendances[0]["artist_id"] == 1
    assert repo.artist_names() == ["Jpegmafia"]


def test_quoted_field_may_span_lines():
    repo = FakeMusicRepository()
    svc.import_concerts(deps(repo, {LOCS.concerts_csv: CSV_HEADER + 'Act,2020-01-01,,Hall,"line one\nline two"\n'}))
    assert repo.attendances[0]["notes"] == "line one\nline two"


def test_a_bad_rules_file_fails_the_step_before_any_write_commits():
    repo = FakeMusicRepository()
    d = deps(repo, {LOCS.concerts_csv: CSV_HEADER, RULES_PATH: json.dumps({"version": 9})})
    with pytest.raises(RuntimeError):
        svc.import_concerts(d)
    assert repo.commits == 0


# ---------------------------------------------------------------- ratings
def test_ratings_prefer_localized_names_unescape_and_share_artists():
    csv_text = (RYM_HEADER
                + rym_row("Bj&ouml;rk", "", "Homogenic &amp; More", 1997, 9, "A1")
                + ",,Sigur,Rós,Ágætis,1999,8,A2\n"
                + rym_row("", "", "No Artist", rym="A3"))
    repo = FakeMusicRepository(artists=[(1, "Björk")])
    result = svc.import_ratings(deps(repo, {LOCS.rym_export_csv: csv_text}))
    assert result.counts == {"albums": 2, "artists": 2}
    assert result.lines == ("[music_ratings] inserted 2 albums", "  artists: 2")
    assert repo.albums[0]["artist_id"] == 1 and repo.albums[0]["title"] == "Homogenic & More"
    assert repo.artist_names() == ["Björk", "Sigur Rós"]


def test_ratings_rerun_replaces_this_exports_albums():
    repo = FakeMusicRepository()
    d = deps(repo, {LOCS.rym_export_csv: RYM_HEADER + rym_row("A", "B", "T")})
    svc.import_ratings(d)
    svc.import_ratings(d)
    assert len(repo.albums) == 1


# ---------------------------------------------------------------- scrobbles
def page(*tracks):
    return [{"artist": {"#text": a}, "name": n, "album": {"#text": al}, "date": {"uts": str(u)}}
            for a, n, al, u in tracks]


def test_absent_scrobble_dump_skips_without_touching_anything():
    repo = FakeMusicRepository()
    result = svc.import_scrobbles(deps(repo))
    assert result.skipped and result.lines == ("[scrobbles] /in/scrobbles.json not found, skipping",)
    assert repo.commits == 0 and repo.sources == {} and repo.forgotten == []


def test_scrobbles_counts_dedupe_and_new_artists_and_tracks():
    pages = [page(("Artist A", "Song", "Alb", 1000), ("artist a", "Song", "Alb", 2000), ("Artist B", "Other", "", 3000)),
             [{"artist": {"#text": "Artist A"}, "name": "Now Playing", "album": {"#text": ""}}]]   # no date: dropped
    repo = FakeMusicRepository(artists=[(1, "ARTIST A")])
    repo.track_rows = [(7, 1, "Song", "Alb")]
    result = svc.import_scrobbles(deps(repo, {LOCS.scrobbles_json: json.dumps(pages)}))
    assert result.counts == {"parsed": 3, "new_artists": 1, "new_tracks": 1, "scrobbles": 3, "tracks": 2, "artists": 2}
    assert result.lines == ("[scrobbles] parsed 3 scrobbles, 1 new artists, 1 new tracks",
                            "  scrobbles: 3, tracks: 2, artists: 2")
    assert repo.commits == 1


def test_scrobbles_rerun_replaces_this_dumps_plays_and_keeps_other_sources():
    repo = FakeMusicRepository()
    other = repo.import_source("/other.json")
    repo.scrobbles_by_source[other] = {(99, "2000-01-01T00:00:00Z")}
    d = deps(repo, {LOCS.scrobbles_json: json.dumps([page(("A", "S", "", 1000))])})
    svc.import_scrobbles(d)
    svc.import_scrobbles(d)
    assert repo.scrobbles_by_source[other] == {(99, "2000-01-01T00:00:00Z")}
    assert repo.count("scrobbles") == 2 and repo.count("tracks") == 1


# ---------------------------------------------------------------- members
def test_seed_members_creates_missing_artists_and_counts_only_new_links():
    rules = {"aliases": {"the duo": "Duo"}, "members": {"The Duo": ["Alice", "bob"]}}
    repo = FakeMusicRepository(artists=[(1, "Alice")])
    d = deps(repo, rules=rules)
    first = svc.seed_artist_members(d)
    assert first.counts == {"artist_members": 2}
    assert first.lines == ("[seed_artist_members] inserted 2 artist_members rows",)
    assert repo.artist_names() == ["Alice", "Duo", "bob"]
    assert svc.seed_artist_members(d).counts == {"artist_members": 0}     # INSERT OR IGNORE: nothing new


def test_seed_members_without_rules_inserts_nothing_but_still_commits():
    repo = FakeMusicRepository()
    assert svc.seed_artist_members(deps(repo)).counts == {"artist_members": 0}
    assert repo.commits == 1
