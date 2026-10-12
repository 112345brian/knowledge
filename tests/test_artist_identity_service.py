"""Artist identity (`client.artist_identity_service` over `client.artist_directory`), against the in-memory music fake.

Moved from tests/test_identity_service.py when artist identity became client logic: each test is unchanged."""
from client import music_records as mu
from client import artist_rules
from client.artist_identity_service import ArtistIdentity
from fake_client_repositories import FakeClientState, FakeMusic


def artists(*names, aliases=None, members=None):
    state = FakeClientState()
    music = FakeMusic(state)
    for n in names:
        music.add_artist(n)
    return state, music, ArtistIdentity(music, mu.ArtistRules(aliases or {}, members or {}))


def test_artist_names_match_case_insensitively_with_python_lower():
    state, music, ids = artists("Jpegmafia")
    assert ids.resolve("JPEGMAFIA") == 1 and len(state.artists) == 1
    assert ids.resolve("Ünter") == ids.resolve("ÜNTER")        # non-ASCII folds too: not SQLite NOCASE


def test_the_first_row_of_a_case_variant_group_wins_the_cache():
    state, music, ids = artists("Beck", "BECK", "beck")
    assert ids.resolve("bEcK") == 1 and len(ids) == 1


def test_html_is_unescaped_then_the_alias_table_is_applied():
    state, music, ids = artists(aliases={"a & b": "A and B"})
    a = ids.resolve("A &amp; B")
    assert state.artists[a] == "A and B" and ids.resolve("a and b") == a


def test_a_created_artist_is_cached_and_len_counts_distinct_keys():
    state, music, ids = artists("Known")
    start = len(ids)
    new = ids.resolve("New")
    assert ids.resolve("new") == new and len(ids) == start + 1
    assert state.operations.count(("music.add_artist", "New")) == 1


def test_the_cache_is_loaded_once_so_a_later_external_insert_is_not_seen():
    """Kept as is: nothing refreshes it (one step, one run)."""
    state, music, ids = artists()
    music.add_artist("Late")
    assert ids.resolve("late") == 2 and len(state.artists) == 2      # a second row, as the legacy cache would make


def test_seed_members_creates_missing_artists_and_counts_only_new_links():
    state, music, ids = artists("Solo", members={"Group": ("Solo", "Other")})
    assert ids.seed_members() == 2
    assert ids.seed_members() == 0
    assert sorted(state.artists.values()) == ["Group", "Other", "Solo"]


def test_artist_rules_from_mapping():
    got = artist_rules.artist_rules_from_mapping({"version": 1, "aliases": {"x": "Y"}, "members": {"G": ["a", "b"]}})
    assert got == mu.ArtistRules({"x": "Y"}, {"G": ("a", "b")}, ())
