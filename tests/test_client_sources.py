"""Optional client sources: the pipeline plan, the schema fragments and the core schema standing alone.
`music`, `measurements` and `claims` are data a given client may not have; the core knows nothing about them."""
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
    assert build_rules.CLIENT_SOURCES == ("music", "measurements", "claims")
    assert set(build_rules.SCHEMA_FRAGMENTS) <= set(build_rules.CLIENT_SOURCES)
    for path in build_rules.SCHEMA_FRAGMENTS.values():
        assert os.path.isfile(os.path.join(REPO, path)), path


def test_steps_with_no_client_source_are_the_core_steps_in_order():
    steps = build_rules.steps_for(())
    assert steps == [p for p, source in build_rules.PIPELINE if source is None]
    assert not any(any(w in s for w in ("concerts", "scrobbles", "music_ratings", "measurements", "seed_claims", "seed_artist")) for s in steps)


def test_enabling_a_source_adds_exactly_its_steps_without_reordering():
    everything = build_rules.steps_for(build_rules.CLIENT_SOURCES)
    assert everything == [p for p, _ in build_rules.PIPELINE]
    for source in build_rules.CLIENT_SOURCES:
        mine = {p for p, s in build_rules.PIPELINE if s == source}
        got = build_rules.steps_for((source,))
        assert mine <= set(got) and set(got) - mine == set(build_rules.steps_for(()))
        assert got == [p for p in everything if p in set(got)]            # same relative order as the full pipeline


def test_every_pipeline_step_exists_and_is_listed_once():
    paths = [p for p, _ in build_rules.PIPELINE]
    assert len(paths) == len(set(paths))
    for p in paths:
        assert os.path.isfile(os.path.join(REPO, p)), p


def test_unknown_and_repeated_sources_are_refused_naming_them():
    with pytest.raises(ValueError, match="unknown client source.*'concert'"):
        build_rules.check_client_sources(("music", "concert"))
    with pytest.raises(ValueError, match="twice"):
        build_rules.check_client_sources(("music", "music"))
    assert build_rules.check_client_sources([]) == ()


def test_schema_files_are_the_core_then_each_enabled_fragment():
    assert build_rules.schema_files(()) == ["schema.sql"]
    assert build_rules.schema_files(("claims",)) == ["schema.sql"]                  # claims needs no tables of its own
    assert build_rules.schema_files(("measurements", "music")) == ["schema.sql", "client/music.sql", "client/measurements.sql"]


def test_the_core_schema_stands_alone_without_any_client_table():
    names = tables(core_con())
    for client_table in ("artists", "venues", "festivals", "concert_attendances", "albums", "tracks", "scrobbles", "import_sources",
                         "artist_members", "metrics", "measurements", "fact_measurements", "exercises", "training_sets", "foods",
                         "food_log_entries", "meal_log_entries", "muscles", "muscle_volume_weekly"):
        assert client_table not in names, client_table
    assert {"facts", "sources", "subjects", "claims", "claim_facts", "fact_revisions", "entities"} <= names


def test_each_fragment_applies_on_the_core_alone_and_together_they_are_the_old_schema():
    for source, path in build_rules.SCHEMA_FRAGMENTS.items():
        con = core_con()
        con.executescript(open(os.path.join(REPO, path)).read())
        assert tables(con) > tables(core_con()), source
    added = set()
    for path in build_rules.SCHEMA_FRAGMENTS.values():
        con = core_con()
        con.executescript(open(os.path.join(REPO, path)).read())
        added |= tables(con) - tables(core_con())
    everything = sqlite3.connect(":memory:")
    everything.executescript(full_schema())
    assert tables(everything) == tables(core_con()) | added and len(added) == 19


def test_the_report_counts_only_tables_the_build_made():
    assert build_rules.report_tables(["facts", "sources"]) == ["sources", "facts"]       # in report order, nothing missing
    assert "artists" not in build_rules.report_tables(tables(core_con()))
    assert "artists" in build_rules.report_tables(tables(sqlite3.connect(":memory:")) | {"artists"})


def test_no_core_object_refers_to_a_client_table():
    """A core view, trigger or index naming a client table would make the core schema fail to load on its own."""
    con = core_con()
    names = tables(con)
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE sql LIKE '%artists%' OR sql LIKE '%scrobbles%'").fetchone()[0] == 0
    assert "measurements" not in " ".join(r[0] or "" for r in con.execute("SELECT sql FROM sqlite_master WHERE type IN ('view','trigger','index')"))
    assert names
