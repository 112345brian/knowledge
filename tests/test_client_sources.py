"""Optional client sources: the pipeline plan, the schema fragments and the core schema standing alone.
`concerts`, `ratings`, `scrobbles`, `measurements` and `claims` are data a given client may not have; the core knows
nothing about them."""
import client_bootstrap
import os
import sqlite3

import pytest

import build_rules
from schema_helper import REPO, full_schema


def core_con():
    con = sqlite3.connect(":memory:")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    return con


def tables(con):
    return {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_the_known_sources_and_their_fragments_are_pinned():
    assert client_bootstrap.registry().names == ("concerts", "ratings", "scrobbles", "measurements", "claims")
    for path in client_bootstrap.registry().schema_fragments():
        assert os.path.isfile(os.path.join(REPO, path)), path
    needing = {i.setting for n in client_bootstrap.registry().names for i in client_bootstrap.registry().source(n).inputs if i.required}
    assert needing == {"CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON"}


def test_steps_with_no_client_source_are_the_core_steps_in_order():
    steps = client_bootstrap.registry().steps_for(())
    assert steps == list(build_rules.CORE_PIPELINE)
    assert not any(s.startswith("client/") for s in steps)


def test_enabling_a_source_adds_exactly_its_steps_without_reordering():
    everything = client_bootstrap.registry().steps_for(client_bootstrap.registry().names)
    assert [p for p in everything if not p.startswith("client/")] == list(build_rules.CORE_PIPELINE)
    for source in client_bootstrap.registry().names:
        got = client_bootstrap.registry().steps_for((source,))
        mine = set(got) - set(client_bootstrap.registry().steps_for(()))
        assert mine, source
        assert got == [p for p in everything if p in set(got)]            # same relative order as the full pipeline
    assert "client/seed_artist_members.py" in client_bootstrap.registry().steps_for(("scrobbles",))   # any music source brings it
    assert "client/seed_artist_members.py" not in client_bootstrap.registry().steps_for(("measurements", "claims"))
    assert "client/concerts.py" not in client_bootstrap.registry().steps_for(("scrobbles", "ratings"))


def test_every_pipeline_step_exists_and_is_listed_once():
    paths = client_bootstrap.registry().all_steps()
    assert len(paths) == len(set(paths))
    for p in paths:
        assert os.path.isfile(os.path.join(REPO, p)), p


def test_unknown_and_repeated_sources_are_refused_naming_them():
    with pytest.raises(ValueError, match="unknown client source.*'concert'"):
        client_bootstrap.registry().check(("concerts", "concert"))
    with pytest.raises(ValueError, match="twice"):
        client_bootstrap.registry().check(("concerts", "concerts"))
    assert client_bootstrap.registry().check([]) == ()


def test_schema_files_are_the_core_then_each_enabled_fragment():
    assert client_bootstrap.registry().schema_files(()) == ["schema.sql"]
    assert client_bootstrap.registry().schema_files(("claims",)) == ["schema.sql"]                  # claims needs no tables of its own
    assert client_bootstrap.registry().schema_files(("measurements", "scrobbles")) == ["schema.sql", "client/music.sql", "client/measurements.sql"]
    assert client_bootstrap.registry().schema_files(("concerts", "ratings", "scrobbles")) == ["schema.sql", "client/music.sql"]   # shared, applied once


def test_the_core_schema_stands_alone_without_any_client_table():
    names = tables(core_con())
    for client_table in ("artists", "venues", "festivals", "concert_attendances", "albums", "tracks", "scrobbles", "import_sources",
                         "artist_members", "metrics", "measurements", "fact_measurements", "exercises", "training_sets", "foods",
                         "food_log_entries", "meal_log_entries", "muscles", "muscle_volume_weekly"):
        assert client_table not in names, client_table
    assert {"facts", "sources", "subjects", "claims", "claim_facts", "fact_revisions", "entities"} <= names


def test_each_fragment_applies_on_the_core_alone_and_together_they_are_the_old_schema():
    for path in client_bootstrap.registry().schema_fragments():
        con = core_con()
        con.executescript(open(os.path.join(REPO, path)).read())
        assert tables(con) > tables(core_con()), path
    added = set()
    for path in client_bootstrap.registry().schema_fragments():
        con = core_con()
        con.executescript(open(os.path.join(REPO, path)).read())
        added |= tables(con) - tables(core_con())
    everything = sqlite3.connect(":memory:")
    everything.executescript(full_schema())
    assert tables(everything) == tables(core_con()) | added and len(added) == 19


def test_the_report_counts_only_tables_the_build_made():
    assert client_bootstrap.registry().report_tables(["facts", "sources"], build_rules.CORE_REPORT_TABLES) == ["sources", "facts"]       # in report order, nothing missing
    assert "artists" not in client_bootstrap.registry().report_tables(tables(core_con()), build_rules.CORE_REPORT_TABLES)
    assert "artists" in client_bootstrap.registry().report_tables(tables(sqlite3.connect(":memory:")) | {"artists"}, build_rules.CORE_REPORT_TABLES)


def test_no_core_object_refers_to_a_client_table():
    """A core view, trigger or index naming a client table would make the core schema fail to load on its own."""
    con = core_con()
    names = tables(con)
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE sql LIKE '%artists%' OR sql LIKE '%scrobbles%'").fetchone()[0] == 0
    assert "measurements" not in " ".join(r[0] or "" for r in con.execute("SELECT sql FROM sqlite_master WHERE type IN ('view','trigger','index')"))
    assert names


# ------------------------------------------------------------------ a real build, with and without each source

import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from pipeline_world import run_pipeline  # noqa: E402

MUSIC_TABLES = {"artists", "venues", "festivals", "concert_attendances", "albums", "tracks", "scrobbles", "import_sources", "artist_members"}
MEASUREMENT_TABLES = {"metrics", "measurements", "fact_measurements", "exercises", "training_sets", "foods", "food_log_entries",
                      "meal_log_entries", "muscles", "muscle_volume_weekly"}


def built(tmp_path, sources):
    dump = run_pipeline(tmp_path, sources=sources)
    return dump, {t for t, d in dump.items() if True}, {t for t, d in dump.items() if d["rows"]}


def test_a_core_only_build_has_no_client_table_and_still_builds_everything_else(tmp_path):
    dump, names, filled = built(tmp_path, ())
    assert not names & (MUSIC_TABLES | MEASUREMENT_TABLES), sorted(names & (MUSIC_TABLES | MEASUREMENT_TABLES))
    assert {"sources", "facts", "fact_revisions", "fact_sources", "subjects", "vault_files", "entities"} <= names
    assert {"sources", "facts", "fact_revisions", "fact_sources", "vault_files"} <= filled
    assert dump["claims"]["rows"] == []                                        # the author's hand-authored claims are the claims source's


def test_each_source_adds_only_the_tables_it_needs(tmp_path):
    core = built(tmp_path / "core", ())[1]
    for source in ("concerts", "ratings", "scrobbles"):
        assert built(tmp_path / source, (source,))[1] - core == MUSIC_TABLES, source        # they share the music tables
    assert built(tmp_path / "meas", ("measurements",))[1] - core == MEASUREMENT_TABLES
    assert built(tmp_path / "claims", ("claims",))[1] == core                              # claims adds rows, not tables


def test_a_music_source_alone_fills_only_its_own_rows(tmp_path):
    _, _, concerts = built(tmp_path / "c", ("concerts",))
    _, _, scrobbles = built(tmp_path / "s", ("scrobbles",))
    _, _, ratings = built(tmp_path / "r", ("ratings",))
    assert {"concert_attendances", "venues"} <= concerts and not {"scrobbles", "tracks", "albums"} & concerts
    assert {"scrobbles", "tracks"} <= scrobbles and not {"concert_attendances", "albums"} & scrobbles
    assert "albums" in ratings and not {"concert_attendances", "scrobbles"} & ratings


def test_the_claims_source_adds_the_claims_and_the_subject_tree(tmp_path):
    dump, _, filled = built(tmp_path, ("claims",))
    assert "claims" in filled and "claim_facts" in filled
    parents = [r for r in dump["subjects"]["rows"] if r[dump["subjects"]["columns"].index("parent_id")] is not None]
    assert parents


def test_the_measurements_source_links_facts_to_measurements(tmp_path):
    dump, _, filled = built(tmp_path, ("measurements",))
    assert {"measurements", "metrics", "fact_measurements"} <= filled


def test_a_build_with_every_source_has_the_rows_each_one_adds(tmp_path):
    _, names, filled = built(tmp_path, client_bootstrap.registry().names)
    assert (MUSIC_TABLES | MEASUREMENT_TABLES) <= names
    assert {"albums", "concert_attendances", "scrobbles", "measurements", "claims"} <= filled


def test_an_enabled_source_without_its_input_path_is_named():
    assert client_bootstrap.registry().missing_inputs(("concerts", "scrobbles", "claims"), ["CONCERTS_CSV"]) == [("scrobbles", "SCROBBLES_JSON")]
    assert client_bootstrap.registry().missing_inputs(("measurements", "claims"), []) == []              # these read no extra path
    assert client_bootstrap.registry().missing_inputs((), []) == []
    with pytest.raises(ValueError, match="unknown client source"):
        client_bootstrap.registry().missing_inputs(("nope",), [])


def test_the_build_names_the_sources_it_includes_and_flags_a_guess():
    import build
    assert "none (core only" in client_bootstrap.registry().note(())
    assert client_bootstrap.registry().note(("concerts", "claims")) == "[build] client sources: concerts, claims"
    guessed = client_bootstrap.registry().note(("concerts", "claims"), implicit=True)
    assert "assumed" in guessed and "CLIENT_SOURCES = ('concerts', 'claims')" in guessed
