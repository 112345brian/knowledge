"""The music client steps as application services, against the client's fake `MusicRepository` and a fake
Inputs: canonical artist identity, the per-run directory, replacement of a source's own rows, festival handling,
the reported counts and when the unit of work commits."""
import json

import pytest

from client import music_records as r
from client.artist_directory import ArtistDirectory
from client.artist_rules import parse_artist_rules
from fake_build_repositories import fake_session
from fake_client_repositories import FakeClientState, FakeMusic
from locations import Locations
from music_fakes import FakeInputs
from client import music_service as svc

CSV_HEADER = "Concert,Start Date,End Date,Location,Notes\n"
DATA = "/data"
LOCS = Locations(knowledge_db_dir="/db", private_data_dir=DATA, client_inputs={"CONCERTS_CSV": "/in/concerts.csv",
                                "RYM_EXPORT_CSV": "/in/rym.csv", "SCROBBLES_JSON": "/in/scrobbles.json"})
RULES_PATH = f"{DATA}/artist_rules.json"


def world(*artists, files=None, rules=None, locations=LOCS):
    """(state, deps): a fake build whose artists table already holds `artists`, in id order."""
    state = FakeClientState()
    session = fake_session(state)
    music = FakeMusic(state)
    for name in artists:
        music.add_artist(name)
    files = dict(files or {})
    if rules is not None:
        files[RULES_PATH] = json.dumps({"version": 1, **rules})
    deps = svc.MusicIngestDeps(music=music, unit_of_work=session.unit_of_work, inputs=FakeInputs(files),
                               locations=locations)
    return state, deps


def names(state):
    return list(state.artists.values())


def rym_row(first, last, title, year=2001, rating=8, rym="Album1"):
    return f"{first},{last},,,{title},{year},{rating},{rym}\n"


# album_artist_name reads ' First Name' (leading space), 'Last Name', 'First Name localized' and ' Last Name localized'
RYM_HEADER = " First Name,Last Name,First Name localized, Last Name localized,Title,Release_Date,Rating,RYM Album\n"


# ---------------------------------------------------------------- rules and directory
def test_no_rules_file_means_no_rules():
    assert svc.load_artist_rules(FakeInputs(), LOCS) == r.ArtistRules({}, {}, ())


def test_rules_file_must_be_version_1_with_aliases_and_members():
    for bad in ({"version": 2, "aliases": {}, "members": {}}, {"version": 1, "aliases": [], "members": {}},
                {"version": 1, "aliases": {}}, {"version": 1, "aliases": {}, "members": {}, "festivals": "x"},
                {"version": 1, "aliases": {}, "members": {"G": 5}}):
        with pytest.raises(RuntimeError, match="expected version 1 with aliases and members"):
            svc.load_artist_rules(FakeInputs({RULES_PATH: json.dumps(bad)}), LOCS)
    with pytest.raises(RuntimeError, match="cannot read artist rules"):
        svc.load_artist_rules(FakeInputs({RULES_PATH: "{not json"}), LOCS)


def test_parse_artist_rules_builds_the_one_record():
    got = parse_artist_rules({"version": 1, "aliases": {"a": "A"}, "members": {"G": ["m"]}, "festivals": ["F"]}, "p")
    assert got == r.ArtistRules({"a": "A"}, {"G": ("m",)}, ("F",))
    assert parse_artist_rules({"version": 1, "aliases": {}, "members": {}}, "p").festivals == ()


def test_directory_resolves_case_insensitively_unescapes_and_applies_aliases():
    state, deps = world("JPEGMAFIA")
    d = ArtistDirectory(deps.music, {"the band": "Band, The"})
    assert d.resolve("Jpegmafia") == 1                      # case-folded onto the existing row
    assert d.resolve("Simon &amp; Garfunkel") == d.resolve("simon & garfunkel")
    assert d.resolve("THE BAND") == d.resolve("band, the")  # alias, then folded
    assert names(state) == ["JPEGMAFIA", "Simon & Garfunkel", "Band, The"]
    assert len(d) == 3


def test_first_existing_row_wins_when_two_differ_only_by_case():
    _, deps = world("Foo", "FOO")
    assert ArtistDirectory(deps.music, None).resolve("foo") == 1


def test_a_directory_lives_one_run_and_is_never_shared():
    _, deps = world()
    first = ArtistDirectory(deps.music, None)
    first.resolve("New Act")
    second = ArtistDirectory(deps.music, None)      # a later run sees it through the repository, not the object
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
    state, deps = world(files={LOCS.client_inputs["CONCERTS_CSV"]: csv_text},
                        rules={"aliases": {}, "members": {}, "festivals": ["Bonnaroo"]})
    result = svc.import_concerts(deps)
    assert result.step == "concerts" and not result.skipped
    assert result.counts == {"concert_attendances": 4, "artists": 5, "venues": 3, "festivals": 1,
                             "unresolved_festivals": 1}
    assert result.lines == (
        "[concerts] inserted 4 concert_attendances",
        "  artists: 5, venues: 3, festivals: 1",
        "  festival rows with no resolvable festival name: 1 (Location held a bare city or similar -- not guessed)",
        "  billing=festival-set: 3",
        "  billing=opener: 1")
    assert names(state) == ["Bonnaroo", "Phoebe Bridgers", "Boygenius", "Big Thief", "Some Band"]
    bonnaroo, phoebe, boygenius, some = state.attendances
    assert bonnaroo.festival_id == phoebe.festival_id == 1
    assert boygenius.billing == "opener" and boygenius.supporting_for_artist_id == 4
    assert boygenius.notes == "opened for Big Thief; great" and boygenius.domain == "music"
    assert some.billing == "festival-set" and some.festival_id is None   # festival named only in the notes
    assert state.commits == 1


def test_the_calls_are_made_in_the_order_ids_depend_on():
    state, deps = world(files={LOCS.client_inputs["CONCERTS_CSV"]: CSV_HEADER + "Boygenius,2023-05-01,,Hall,opened for Big Thief\n"})
    svc.import_concerts(deps)
    assert [op[0] for op in state.operations] == [
        "music.delete_attendances", "music.add_artist", "music.add_artist", "music.add_attendance", "commit"]
    assert [(op[1]) for op in state.operations if op[0] == "music.add_artist"] == ["Boygenius", "Big Thief"]


def test_a_rerun_replaces_only_this_sources_attendances():
    state, deps = world("Old", files={LOCS.client_inputs["CONCERTS_CSV"]: CSV_HEADER + "Act,2020-01-01,,Hall,\n"})
    other = deps.music.import_source("/elsewhere.csv")
    deps.music.add_attendance(r.NewAttendance(r.ArtistId(1), "2000-01-01", None, None, None, "headliner", None, None,
                                              other))
    svc.import_concerts(deps)
    svc.import_concerts(deps)
    mine = state.import_sources[LOCS.client_inputs["CONCERTS_CSV"]]
    assert [a.import_source_id for a in state.attendances].count(mine) == 1     # not doubled
    assert [a.import_source_id for a in state.attendances].count(other) == 1    # untouched
    assert names(state) == ["Old", "Act"]                                       # not re-created


def test_concert_names_resolve_through_canonical_artist_identity():
    state, deps = world("Jpegmafia", files={LOCS.client_inputs["CONCERTS_CSV"]: CSV_HEADER + "JPEGMAFIA,2020-01-01,,Hall,\n"})
    svc.import_concerts(deps)
    assert state.attendances[0].artist_id == 1 and names(state) == ["Jpegmafia"]


def test_quoted_field_may_span_lines():
    state, deps = world(files={LOCS.client_inputs["CONCERTS_CSV"]: CSV_HEADER + 'Act,2020-01-01,,Hall,"line one\nline two"\n'})
    svc.import_concerts(deps)
    assert state.attendances[0].notes == "line one\nline two"


def test_a_bad_rules_file_fails_the_step_before_any_write_commits():
    state, deps = world(files={LOCS.client_inputs["CONCERTS_CSV"]: CSV_HEADER, RULES_PATH: json.dumps({"version": 9})})
    with pytest.raises(RuntimeError):
        svc.import_concerts(deps)
    assert state.commits == 0 and state.attendances == [] and state.artists == {}


# ---------------------------------------------------------------- ratings
def test_ratings_prefer_localized_names_unescape_and_share_artists():
    csv_text = (RYM_HEADER
                + rym_row("Bj&ouml;rk", "", "Homogenic &amp; More", 1997, 9, "A1")
                + ",,Sigur,Rós,Ágætis,1999,8,A2\n"
                + rym_row("", "", "No Artist", rym="A3"))
    state, deps = world("Björk", files={LOCS.client_inputs["RYM_EXPORT_CSV"]: csv_text})
    result = svc.import_ratings(deps)
    assert result.counts == {"albums": 2, "artists": 2}
    assert result.lines == ("[music_ratings] inserted 2 albums", "  artists: 2")
    assert state.albums[0].artist_id == 1 and state.albums[0].title == "Homogenic & More"
    assert (state.albums[0].release_year, state.albums[0].rating, state.albums[0].rym_id) == (1997, 9, "A1")
    assert names(state) == ["Björk", "Sigur Rós"]
    assert state.commits == 1


def test_ratings_rerun_replaces_this_exports_albums():
    state, deps = world(files={LOCS.client_inputs["RYM_EXPORT_CSV"]: RYM_HEADER + rym_row("A", "B", "T")})
    svc.import_ratings(deps)
    svc.import_ratings(deps)
    assert len(state.albums) == 1


# ---------------------------------------------------------------- scrobbles
def page(*tracks):
    return [{"artist": {"#text": a}, "name": n, "album": {"#text": al}, "date": {"uts": str(u)}}
            for a, n, al, u in tracks]


def test_absent_scrobble_dump_skips_without_touching_anything():
    state, deps = world()
    result = svc.import_scrobbles(deps)
    assert result.skipped and result.lines == ("[scrobbles] /in/scrobbles.json not found, skipping",)
    assert state.commits == 0 and state.import_sources == {} and state.operations == []


def test_scrobbles_counts_dedupe_and_new_artists_and_tracks():
    pages = [page(("Artist A", "Song", "Alb", 1000), ("artist a", "Song", "Alb", 2000), ("Artist B", "Other", "", 3000)),
             [{"artist": {"#text": "Artist A"}, "name": "Now Playing", "album": {"#text": ""}}]]   # no date: dropped
    state, deps = world("ARTIST A", files={LOCS.client_inputs["SCROBBLES_JSON"]: json.dumps(pages)})
    state.tracks[7] = (1, "Song", "Alb")
    result = svc.import_scrobbles(deps)
    assert result.counts == {"parsed": 3, "new_artists": 1, "new_tracks": 1, "scrobbles": 3, "tracks": 2, "artists": 2}
    assert result.lines == ("[scrobbles] parsed 3 scrobbles, 1 new artists, 1 new tracks",
                            "  scrobbles: 3, tracks: 2, artists: 2")
    assert state.commits == 1
    assert [(s.track_id, s.played_at) for s in state.scrobbles] == [
        (7, "1970-01-01T00:16:40Z"), (7, "1970-01-01T00:33:20Z"), (8, "1970-01-01T00:50:00Z")]


def test_scrobbles_rerun_replaces_this_dumps_plays_and_keeps_other_sources():
    state, deps = world(files={LOCS.client_inputs["SCROBBLES_JSON"]: json.dumps([page(("A", "S", "", 1000))])})
    other = deps.music.import_source("/other.json")
    deps.music.add_scrobbles([r.NewScrobble(r.TrackId(99), "2000-01-01T00:00:00Z", other)])
    svc.import_scrobbles(deps)
    svc.import_scrobbles(deps)
    assert [s.import_source_id for s in state.scrobbles].count(other) == 1
    assert deps.music.totals().scrobbles == 2 and deps.music.totals().tracks == 1


# ---------------------------------------------------------------- members
def test_seed_members_creates_missing_artists_and_counts_only_new_links():
    rules = {"aliases": {"the duo": "Duo"}, "members": {"The Duo": ["Alice", "bob"]}}
    state, deps = world("Alice", rules=rules)
    first = svc.seed_artist_members(deps)
    assert first.counts == {"artist_members": 2}
    assert first.lines == ("[seed_artist_members] inserted 2 artist_members rows",)
    assert names(state) == ["Alice", "Duo", "bob"]
    assert state.artist_members == [(2, 1), (2, 3)]
    assert svc.seed_artist_members(deps).counts == {"artist_members": 0}     # INSERT OR IGNORE: nothing new


def test_seed_members_without_rules_inserts_nothing_but_still_commits():
    state, deps = world()
    assert svc.seed_artist_members(deps).counts == {"artist_members": 0}
    assert state.commits == 1


def test_a_step_whose_input_is_not_configured_names_the_setting():
    bare = Locations(knowledge_db_dir="/db", private_data_dir=DATA)
    for fn, setting in ((svc.import_concerts, "CONCERTS_CSV"), (svc.import_ratings, "RYM_EXPORT_CSV"),
                        (svc.import_scrobbles, "SCROBBLES_JSON")):
        state, deps = world(locations=bare)
        with pytest.raises(ValueError, match=setting):
            fn(deps)
        assert state.import_sources == {} and state.commits == 0
