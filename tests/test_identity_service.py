"""The shared identity operations (identity_service / identity_rules), against the in-memory repositories.

Each test states one frozen rule and names the legacy behaviour it reproduces, so a lane can delete the step's
own helper knowing this is the single definition. A last test pins the legacy duplicates that still exist; it is
a ratchet: a lane that migrates a step removes its entry and the test fails if one is left or a new one appears.
"""
import ast
import os
import sqlite3

import pytest

import build_records as r
import identity_rules as rules
import identity_service as ident
from fake_build_repositories import CollectingDiagnostics, FakeInputs, FakeState, fake_session

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source(citekey, **kw):
    return r.NewSource(citekey, kw.pop("name", citekey or "n"), "primary", **kw)


# ---------------------------------------------------------------------------------------- source identity

def test_citekeys_is_a_snapshot_not_a_live_view():
    s = fake_session()
    ids = ident.SourceIdentity(s.sources, CollectingDiagnostics())
    s.sources.add(source("a"))
    before = ids.citekeys()
    s.sources.add(source("b"))
    assert set(before) == {"a"} and set(ids.citekeys()) == {"a", "b"}


def test_claims_skip_a_known_citekey_and_remember_the_ones_it_hands_out():
    """literature_sources: `existing` is read once, `existing.add` after each insert."""
    s = fake_session()
    s.sources.add(source("a"))
    claims = ident.SourceIdentity(s.sources, CollectingDiagnostics()).claims()
    assert [claims.claim(k) for k in ("a", "b", "b", "c")] == [False, True, False, True]


def test_a_source_without_citekey_is_not_known_but_is_remembered_once_claimed():
    """Kept as is (literature_sources): `existing` holds only citekeys from the table, then `existing.add(None)`."""
    s = fake_session()
    s.sources.add(source(None))
    claims = ident.SourceIdentity(s.sources, CollectingDiagnostics()).claims()
    assert [claims.claim(None), claims.claim(None)] == [True, False]


def test_require_names_the_missing_citekey_and_the_hint():
    s = fake_session()
    ids = ident.SourceIdentity(s.sources, CollectingDiagnostics())
    s.sources.add(source("here"))
    assert ids.require("here", "x") == 1
    with pytest.raises(ident.SourceNotFound, match=r"^source citekey not found: gone \(run seed_sources.py first\)$"):
        ids.require("gone", "run seed_sources.py first")
    assert issubclass(ident.SourceNotFound, RuntimeError)       # measurements raised RuntimeError


def test_duplicate_citekey_is_refused_by_the_repository():
    s = fake_session()
    s.sources.add(source("a"))
    with pytest.raises(sqlite3.IntegrityError):
        s.sources.add(source("a"))


def test_an_identifier_another_source_owns_is_a_reported_conflict_never_a_merge():
    state = FakeState()
    s, d = fake_session(state), CollectingDiagnostics()
    ids = ident.SourceIdentity(s.sources, d)
    one, two = s.sources.add(source("one")), s.sources.add(source("two"))
    assert ids.attach_identifiers(one, "one", [("doi", "10.1/x"), ("pmid", "7")]) == []
    conflicts = ids.attach_identifiers(two, "two", [("doi", "10.1/x"), ("isbn", "9")])
    assert conflicts == [r.IdentifierConflict("doi", "10.1/x", "one")]
    assert state.identifiers == {("doi", "10.1/x"): one, ("pmid", "7"): one, ("isbn", "9"): two}
    assert state.identifier_conflicts == [(two, "doi", "10.1/x", one)]
    assert d.lines == ["  WARNING -- duplicate identifier doi:10.1/x shared by sources 'one' and 'two' (not merged)"]


def test_a_source_repeating_its_own_identifier_is_silent():
    state = FakeState()
    s, d = fake_session(state), CollectingDiagnostics()
    ids = ident.SourceIdentity(s.sources, d)
    one = s.sources.add(source("one"))
    ids.attach_identifiers(one, "one", [("doi", "d")])
    assert ids.attach_identifiers(one, "one", [("doi", "d")]) == []
    assert d.lines == [] and state.identifier_conflicts == []


@pytest.mark.parametrize("byline, links", [
    (None, []), ("", []), ("A", [("A", 0)]), ("A; B", [("A", 0), ("B", 1)]),
    (" A ;; B ", [("A", 0), ("B", 2)]),        # a blank still takes a position (kept as is)
    (";", []),
])
def test_author_links_keep_byline_positions(byline, links):
    assert rules.author_links(byline) == links


def test_link_authors_are_ordered_and_unique():
    state = FakeState()
    s = fake_session(state)
    ids = ident.SourceIdentity(s.sources, CollectingDiagnostics())
    a = s.sources.add(source("a"))
    ids.link_authors(a, "X; Y; X")
    assert state.authors == {"X": 1, "Y": 2}
    assert state.source_authors == [(a, 1, 0), (a, 2, 1)]       # the repeat of X is INSERT OR IGNOREd


def test_vault_file_is_fingerprinted_once_and_never_for_an_empty_path():
    state = FakeState()
    s = fake_session(state)
    ids = ident.SourceIdentity(s.sources, CollectingDiagnostics())
    inputs = FakeInputs(fingerprints={"/v/a.md": {"content_sha256": "0" * 64, "size_bytes": 3, "file_mtime": "t",
                                                  "mime_type": "text/markdown", "file_state": "present"}},
                        acquisitions={"/v/a.md": {"acquired_via": "manual"}})
    assert ids.vault_file("", inputs) is None and ids.vault_file(None, inputs) is None
    first = ids.vault_file("/v/a.md", inputs)
    assert ids.vault_file("/v/a.md", inputs) == first
    assert state.operations.count(("sources.record_vault_file_fixity", str(first))) == 1
    fx = state.vault_file_fixity[first]
    assert (fx.content_sha256, fx.file_state, fx.acquired_via) == ("0" * 64, "present", "manual")


def test_an_unreadable_vault_file_is_recorded_missing_and_does_not_raise():
    state = FakeState()
    s = fake_session(state)
    fid = ident.SourceIdentity(s.sources, CollectingDiagnostics()).vault_file("/gone.md", FakeInputs())
    assert state.vault_file_fixity[fid].file_state == "missing" and state.vault_file_fixity[fid].content_sha256 is None


# ---------------------------------------------------------------------------------------- subject identity

def test_subject_resolve_finds_by_name_first_then_creates():
    state = FakeState()
    s = fake_session(state)
    sid = ident.SubjectIdentity(s.subjects)
    a = sid.resolve("alpha")
    assert sid.resolve("alpha") == a and sid.touched == 1
    assert state.operations.count(("subjects.add", "alpha")) == 1


def test_subject_aliases_are_ignored_unless_asked_for():
    """facts.py and measurements.py (name only) vs seed_general_facts.py (name, then alias)."""
    state = FakeState()
    s = fake_session(state)
    canonical = s.subjects.add("canonical")
    s.subjects.add_alias(canonical, "nick")
    assert ident.SubjectIdentity(s.subjects).resolve("nick") != canonical          # a second, separate subject
    assert ident.SubjectIdentity(s.subjects).resolve("nick2", via_aliases=True) != canonical
    assert ident.SubjectIdentity(s.subjects).resolve("nick", via_aliases=True) == \
        s.subjects.find_by_name("nick")          # an exact name still wins over the alias


def test_an_alias_resolves_to_its_canonical_subject_before_anything_is_created():
    state = FakeState()
    s = fake_session(state)
    canonical = s.subjects.add("canonical")
    s.subjects.add_alias(canonical, "nick")
    before = len(state.subjects)
    assert ident.SubjectIdentity(s.subjects).resolve("nick", via_aliases=True) == canonical
    assert len(state.subjects) == before


def test_new_subject_domain_is_the_given_one_or_the_schema_default():
    state = FakeState()
    s = fake_session(state)
    sid = ident.SubjectIdentity(s.subjects)
    a, b = sid.resolve("a"), sid.resolve("b", domain="general")
    assert (state.subjects[a]["domain"], state.subjects[b]["domain"]) == ("health-and-fitness", "general")


def test_the_cache_is_keyed_by_the_name_as_given_and_remembers_the_first_lookup():
    state = FakeState()
    s = fake_session(state)
    sid = ident.SubjectIdentity(s.subjects)
    first = sid.resolve("x", domain="one")
    assert sid.resolve("x", domain="two") == first
    assert state.subjects[first]["domain"] == "one"


# ----------------------------------------------------------------------------------------- artist identity

def artists(*names, aliases=None, members=None):
    state = FakeState()
    s = fake_session(state)
    for n in names:
        s.music.add_artist(n)
    return state, s, ident.ArtistIdentity(s.music, r.ArtistRules(aliases or {}, members or {}))


def test_artist_names_match_case_insensitively_with_python_lower():
    state, s, ids = artists("Jpegmafia")
    assert ids.resolve("JPEGMAFIA") == 1 and len(state.artists) == 1
    assert ids.resolve("Ünter") == ids.resolve("ÜNTER")        # non-ASCII folds too: not SQLite NOCASE


def test_the_first_row_of_a_case_variant_group_wins_the_cache():
    state, s, ids = artists("Beck", "BECK", "beck")
    assert ids.resolve("bEcK") == 1 and len(ids) == 1


def test_html_is_unescaped_then_the_alias_table_is_applied():
    state, s, ids = artists(aliases={"a & b": "A and B"})
    a = ids.resolve("A &amp; B")
    assert state.artists[a] == "A and B" and ids.resolve("a and b") == a


def test_a_created_artist_is_cached_and_len_counts_distinct_keys():
    state, s, ids = artists("Known")
    start = len(ids)
    new = ids.resolve("New")
    assert ids.resolve("new") == new and len(ids) == start + 1
    assert state.operations.count(("music.add_artist", "New")) == 1


def test_the_cache_is_loaded_once_so_a_later_external_insert_is_not_seen():
    """Kept as is: nothing refreshes it (one step, one run)."""
    state, s, ids = artists()
    s.music.add_artist("Late")
    assert ids.resolve("late") == 2 and len(state.artists) == 2      # a second row, as the legacy cache would make


def test_seed_members_creates_missing_artists_and_counts_only_new_links():
    state, s, ids = artists("Solo", members={"Group": ("Solo", "Other")})
    assert ids.seed_members() == 2
    assert ids.seed_members() == 0
    assert sorted(state.artists.values()) == ["Group", "Other", "Solo"]


def test_artist_rules_from_mapping():
    got = rules.artist_rules_from_mapping({"version": 1, "aliases": {"x": "Y"}, "members": {"G": ["a", "b"]}})
    assert got == r.ArtistRules({"x": "Y"}, {"G": ("a", "b")}, ())


# ------------------------------------------------------------------------- one definition each (ratchet)

IDENTITY_FUNCTION_NAMES = {"canonical_artist_name", "artist_key", "get_or_create_artist", "load_artist_cache",
                           "get_or_create_subject", "get_source_id", "link_authors", "get_or_create_author",
                           "add_source_identifiers", "get_or_create_vault_file", "get_or_create_publisher"}
# Legacy definitions that still exist because their step has not moved yet. A lane deletes its entries.
LEGACY_DEFINITIONS = {
    ("ingest/shared.py", "get_or_create_publisher"), ("ingest/shared.py", "get_or_create_author"),
    ("ingest/shared.py", "get_or_create_vault_file"), ("ingest/shared.py", "add_source_identifiers"),
    ("ingest/shared.py", "link_authors"),
    ("ingest/facts.py", "get_or_create_subject"), ("ingest/seed_general_facts.py", "get_or_create_subject"),
    ("client/measurements.py", "get_or_create_subject"), ("client/measurements.py", "get_source_id"),
    ("client/music_shared.py", "load_artist_cache"), ("client/music_shared.py", "get_or_create_artist"),
}


def _definitions():
    found = set()
    for sub in ("", "ingest", "client"):
        folder = os.path.join(REPO, sub)
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(folder, name), encoding="utf-8").read())
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in IDENTITY_FUNCTION_NAMES:
                    found.add((f"{sub}/{name}" if sub else name, node.name))
    return found


def test_identity_functions_have_one_definition_and_no_new_copies_appear():
    defs = _definitions()
    owners = {"canonical_artist_name": "identity_rules.py", "artist_key": "identity_rules.py"}
    for fn, owner in owners.items():
        assert {p for p, n in defs if n == fn} == {owner}, f"{fn} must be defined only in {owner}"
    new = {d for d in defs if d not in LEGACY_DEFINITIONS and d[1] not in owners}
    gone = LEGACY_DEFINITIONS - defs
    assert not new, f"a new copy of an identity operation: {sorted(new)}; use identity_service"
    assert not gone, f"migrated: remove from LEGACY_DEFINITIONS: {sorted(gone)}"
