"""Whole-system integration tests on a synthetic world (tests/pipeline_world.py): the real build steps, the real
adapters and the real normal-db builder, in this process, with nothing ambient.

Every test builds its own world under tmp_path, turns it into explicit `Locations` and hands a `FixedClock` to
whatever needs time. No test reads a developer's private checkout, the wall clock, or KNOWLEDGE_FROZEN_NOW /
KNOWLEDGE_PRIVATE_DIR (the autouse fixture in conftest removes the second; the first is removed here).

* the full and the normal database for each supported combination of optional client sources;
* two containers configured independently in one process leak neither configuration nor time into each other;
* twelve rebuilds under one frozen clock: every backup distinct, the newest five kept;
* a failure before the swap leaves the previous full db (and normal db) byte-identical;
* a failure while generating the normal db leaves the new full db and removes the stale normal one.
"""
import hashlib
import json
import re
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402
import building  # noqa: E402
import build_rules  # noqa: E402
import clock as clocks  # noqa: E402
import client_bootstrap  # noqa: E402
import local_buildfiles  # noqa: E402
import normal_db  # noqa: E402
import paths  # noqa: E402
from pipeline_world import build_world  # noqa: E402

T0 = "2026-10-01T00:00:00+00:00"

MUSIC_TABLES = {"artists", "venues", "festivals", "concert_attendances", "albums", "tracks", "scrobbles",
                "import_sources", "artist_members"}
MEASUREMENT_TABLES = {"metrics", "measurements", "fact_measurements", "exercises", "training_sets", "foods",
                      "food_log_entries", "meal_log_entries", "muscles", "muscle_volume_weekly"}


@pytest.fixture(autouse=True)
def _no_ambient_time_or_checkout(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_FROZEN_NOW", raising=False)
    monkeypatch.delenv("KNOWLEDGE_PRIVATE_DIR", raising=False)


def make_locations(root, monkeypatch, sources=()):
    """Write a world under `root` and return the Locations its local_paths.py describes. The private checkout is
    named only for the one `paths.load()` call, then the environment is clean again."""
    env = build_world(root, sources)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", env["KNOWLEDGE_PRIVATE_DIR"])
    monkeypatch.setenv("KNOWLEDGE_HOME", env["KNOWLEDGE_HOME"])
    try:
        return paths.load()
    finally:
        monkeypatch.delenv("KNOWLEDGE_PRIVATE_DIR")


def rebuild(locations, clock=None, *args):
    """build.main() with the real adapters; returns normally, or raises SystemExit like the command."""
    build.main(locations, list(args), clock or clocks.FixedClock(T0), local_buildfiles)


def table_names(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                if not r[0].startswith(("sqlite_",)) and not r[0].endswith(("_data", "_idx", "_docsize", "_config", "_content"))}
    finally:
        con.close()


def query(path, sql, *args):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree_listing(directory):
    return sorted(os.path.relpath(os.path.join(d, f), directory) for d, _, fs in os.walk(directory) for f in fs)


def fail_during_pipeline(monkeypatch):
    """Fail one real step through the composed registry, after touching only the temporary database."""
    real_run = building.Registry.run

    def run(registry, step, session):
        if step == "ingest/facts.py":
            session.con.execute("CREATE TABLE half_written (x)")
            session.con.commit()
            raise RuntimeError("step exploded")
        return real_run(registry, step, session)

    monkeypatch.setattr(building.Registry, "run", run)


# ------------------------------------------------------------------ full and normal output per source combination

COMBINATIONS = [
    (),
    ("concerts",),
    ("ratings", "scrobbles"),
    ("measurements",),
    ("claims",),
    ("measurements", "claims"),
    client_bootstrap.registry().names,
]


@pytest.mark.parametrize("sources", COMBINATIONS, ids=lambda s: "+".join(s) or "core-only")
def test_a_build_makes_a_full_and_a_normal_db_for_each_supported_source_combination(tmp_path, monkeypatch, sources):
    loc = make_locations(tmp_path, monkeypatch, sources)
    assert loc.client_sources == tuple(sources)
    rebuild(loc)

    full, normal = loc.db_path, os.path.join(loc.db_dir, "knowledge-normal.db")
    assert tree_listing(loc.db_dir) == ["knowledge-normal.db", "knowledge.db"]      # first build: nothing to back up

    have = table_names(full)
    music = set(sources) & {"concerts", "ratings", "scrobbles"}
    assert (MUSIC_TABLES <= have) == bool(music), sorted(MUSIC_TABLES - have)
    assert (MEASUREMENT_TABLES <= have) == ("measurements" in sources), sorted(MEASUREMENT_TABLES - have)
    assert bool(query(full, "SELECT 1 FROM claims LIMIT 1")) == ("claims" in sources)
    assert query(full, "SELECT COUNT(*) FROM facts")[0][0] > 0 and query(full, "SELECT COUNT(*) FROM sources")[0][0] > 0
    assert query(full, "PRAGMA integrity_check") == [("ok",)]
    recorded = query(full, "SELECT * FROM build_info")
    assert recorded and T0[:10] in json.dumps(recorded, default=str)     # the frozen clock, not the wall clock

    # the normal db: only normal-visibility facts, none of the client tables, nothing the full db does not have
    kept = table_names(normal)
    assert kept and kept <= have
    assert not kept & (MUSIC_TABLES | MEASUREMENT_TABLES | {"claims", "claim_facts"}), sorted(kept)
    assert {r[0] for r in query(normal, "SELECT DISTINCT visibility FROM facts")} <= {"normal"}
    full_normal_keys = {r[0] for r in query(full, "SELECT source_key FROM facts WHERE visibility = 'normal'")}
    assert {r[0] for r in query(normal, "SELECT source_key FROM facts")} <= full_normal_keys
    assert query(normal, "SELECT COUNT(*) FROM facts")[0][0] < query(full, "SELECT COUNT(*) FROM facts")[0][0]
    assert query(normal, "PRAGMA integrity_check") == [("ok",)]


def test_the_same_world_and_clock_rebuild_to_the_same_rows(tmp_path, monkeypatch):
    """Determinism: two builds of one world under one frozen clock hold identical rows (file bytes may differ in
    page layout and timestamps SQLite stamps itself, so compare table contents)."""
    loc = make_locations(tmp_path, monkeypatch, client_bootstrap.registry().names)
    rebuild(loc)
    def stable_rows(path, table):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            columns = con.execute(f'PRAGMA table_info("{table}")').fetchall()
            timestamp_columns = {index for index, column in enumerate(columns)
                                if "CURRENT_TIMESTAMP" in str(column[4]).upper()}
            rows = []
            for row in con.execute(f'SELECT * FROM "{table}"'):
                rows.append(tuple(
                    "<CURRENT_TIMESTAMP>"
                    if index in timestamp_columns or (isinstance(value, str) and re.fullmatch(
                        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?", value
                    ))
                    else value
                    for index, value in enumerate(row)
                ))
            return sorted(map(repr, rows))
        finally:
            con.close()

    first = {t: stable_rows(loc.db_path, t) for t in sorted(table_names(loc.db_path)) if t != "build_info"}
    rebuild(loc)
    second = {t: stable_rows(loc.db_path, t) for t in sorted(table_names(loc.db_path)) if t != "build_info"}
    assert first.keys() == second.keys()
    for t in first:
        # CURRENT_TIMESTAMP defaults differ run to run; every other column must match exactly
        assert first[t] == second[t], t


# ------------------------------------------------------------------ two containers, nothing ambient


def test_two_containers_in_one_process_share_neither_configuration_nor_time(tmp_path, monkeypatch):
    import container
    from new_fact import NewFact
    loc_a = make_locations(tmp_path / "a", monkeypatch, ("concerts",))
    loc_b = make_locations(tmp_path / "b", monkeypatch, ())
    assert loc_a.private_data_dir != loc_b.private_data_dir and loc_a.db_dir != loc_b.db_dir
    # an ambient clock and checkout that neither container was given must not reach either of them
    monkeypatch.setenv("KNOWLEDGE_FROZEN_NOW", "1999-12-31T23:59:59+00:00")
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", str(tmp_path / "does-not-exist"))
    clock_a, clock_b = clocks.FixedClock("2026-01-02T03:04:05+00:00"), clocks.FixedClock("2027-06-07T08:09:10+00:00")
    app_a, app_b = container.Container(loc_a, clock_a), container.Container(loc_b, clock_b)

    assert app_a.locations is loc_a and app_b.locations is loc_b
    assert app_a.clock is clock_a and app_b.clock is clock_b
    assert app_a.add_fact.deps.locations is loc_a and app_b.add_fact.deps.locations is loc_b
    assert app_a.add_fact.deps.clock is clock_a and app_b.add_fact.deps.clock is clock_b

    def add(app, statement):
        result = app.add_fact.append_fact(NewFact(statement=statement, subject="topic-a", trust_level="medium",
                                                  recheck_by="2028-01-01", captured_via="cli"))
        assert result.ok, result.errors
        return json.load(open(app.locations.data_path))[-1]

    entry_a, entry_b = add(app_a, "Only world A has this."), add(app_b, "Only world B has this.")
    assert entry_a["date_added"].startswith("2026-01-02") and entry_a["captured_at"].startswith("2026-01-02T03:04:05")
    assert entry_b["date_added"].startswith("2027-06-07") and entry_b["captured_at"].startswith("2027-06-07T08:09:10")
    assert "1999" not in json.dumps([entry_a, entry_b])
    assert "Only world B has this." not in open(loc_a.data_path).read()
    assert "Only world A has this." not in open(loc_b.data_path).read()

    # and a build through each container's own inputs lands in its own directory, stamped by its own clock
    rebuild(loc_a, clock_a)
    rebuild(loc_b, clock_b)
    assert os.path.exists(loc_a.db_path) and os.path.exists(loc_b.db_path)
    assert "2026-01-02" in json.dumps(query(loc_a.db_path, "SELECT * FROM build_info"), default=str)
    assert "2027-06-07" in json.dumps(query(loc_b.db_path, "SELECT * FROM build_info"), default=str)
    assert MUSIC_TABLES <= table_names(loc_a.db_path) and not MUSIC_TABLES & table_names(loc_b.db_path)
    statements_a = {r[0] for r in query(loc_a.db_path, "SELECT statement FROM facts")}
    statements_b = {r[0] for r in query(loc_b.db_path, "SELECT statement FROM facts")}
    assert "Only world A has this." in statements_a and "Only world B has this." not in statements_a
    assert "Only world B has this." in statements_b and "Only world A has this." not in statements_b


# ------------------------------------------------------------------ twelve rebuilds under one frozen clock


def twelve_rebuilds(tmp_path, monkeypatch):
    """Twelve rebuilds of one world under one frozen clock, each with different content. Returns (locations,
    backups directory, [sha256 of the live db after rebuild 1..12])."""
    loc = make_locations(tmp_path, monkeypatch, ())
    frozen = clocks.FixedClock("2026-10-03T08:00:00+00:00")
    live = []
    for n in range(12):
        entries = json.load(open(loc.data_path))
        entries.append({**entries[0], "source_key": f"extra-{n}", "statement": f"Extra fact number {n}."})
        with open(loc.data_path, "w") as f:
            json.dump(entries, f)
        rebuild(loc, frozen)
        live.append(digest(loc.db_path))
    return loc, os.path.join(loc.db_dir, "backups"), live


def test_twelve_rebuilds_under_one_frozen_clock_leave_five_distinct_backups(tmp_path, monkeypatch):
    loc, backups, live = twelve_rebuilds(tmp_path, monkeypatch)
    kept = sorted(os.listdir(backups))
    assert len(kept) == build_rules.KEEP_BACKUPS == 5, kept
    assert all(n.startswith("knowledge.db.bak-20261003T080000") for n in kept), kept     # the frozen second
    held = [digest(os.path.join(backups, n)) for n in kept]
    assert len(set(held)) == 5, "two backups hold the same database: one overwrote another"
    assert set(held) <= set(live[:-1])                           # each is a real earlier build, never the live one
    assert live[-1] not in held and digest(loc.db_path) == live[-1]
    assert query(loc.db_path, "SELECT COUNT(*) FROM facts WHERE statement LIKE 'Extra fact number %'") == [(12,)]


def test_twelve_rebuilds_under_one_frozen_clock_keep_the_five_newest_backups(tmp_path, monkeypatch):
    loc, backups, live = twelve_rebuilds(tmp_path, monkeypatch)
    held = sorted(digest(os.path.join(backups, n)) for n in os.listdir(backups))
    # rebuild n (n >= 2) backs up the live db of rebuild n-1: the newest five backups are the dbs of builds 7..11
    assert held == sorted(live[6:11]), "the kept backups are not the five newest"


# ------------------------------------------------------------------ failure before the swap, and normal-db failure


def test_a_failure_before_publication_preserves_the_previous_full_and_normal_db(tmp_path, monkeypatch, capsys):
    loc = make_locations(tmp_path, monkeypatch, ())
    rebuild(loc)
    normal = os.path.join(loc.db_dir, "knowledge-normal.db")
    full_before, normal_before, listing = digest(loc.db_path), digest(normal), tree_listing(loc.db_dir)

    fail_during_pipeline(monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        rebuild(loc)
    assert exit_info.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "build failed in" in err and "step exploded" in err and "live DB left untouched" in err

    assert digest(loc.db_path) == full_before                    # byte-identical: nothing was swapped in
    assert digest(normal) == normal_before                       # the normal db still belongs to that full db
    assert tree_listing(loc.db_dir) == listing                   # no temp file, no backup, nothing else
    assert "half_written" not in table_names(loc.db_path)


def test_a_failure_before_publication_on_the_first_build_leaves_no_database(tmp_path, monkeypatch):
    loc = make_locations(tmp_path, monkeypatch, ())
    fail_during_pipeline(monkeypatch)
    with pytest.raises(SystemExit):
        rebuild(loc)
    assert not os.path.exists(loc.db_path) and not os.path.exists(os.path.join(loc.db_dir, "knowledge-normal.db"))
    assert [n for n in os.listdir(loc.db_dir)] == [] if os.path.isdir(loc.db_dir) else True


def test_a_normal_generation_failure_invalidates_the_old_normal_db_and_keeps_the_new_full_db(tmp_path, monkeypatch, capsys):
    loc = make_locations(tmp_path, monkeypatch, ())
    rebuild(loc)
    normal = os.path.join(loc.db_dir, "knowledge-normal.db")
    assert os.path.exists(normal)
    full_before = digest(loc.db_path)

    # a new fact makes the next full db differ; the normal build then fails (its leak scan reports a leak)
    entries = json.load(open(loc.data_path))
    entries.append({**entries[0], "source_key": "g-new", "statement": "A fact added before the second build."})
    with open(loc.data_path, "w") as f:
        json.dump(entries, f)
    real_scan = normal_db.normal_db_service.scan_against_full
    monkeypatch.setattr(normal_db.normal_db_service, "scan_against_full",
                        lambda deps, normal_path, full_path: [("private-marker", "private-marker", "facts.statement (injected)")])
    with pytest.raises(SystemExit) as exit_info:
        rebuild(loc)
    assert exit_info.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "knowledge.db was rebuilt, but the normal-only DB failed" in err and "leak test failed" in err

    assert not os.path.exists(normal), "a normal db built from the previous full db was left in place"
    assert os.path.exists(loc.db_path) and digest(loc.db_path) != full_before          # the new full db is published
    assert query(loc.db_path, "SELECT COUNT(*) FROM facts WHERE statement = 'A fact added before the second build.'") == [(1,)]
    assert [n for n in tree_listing(loc.db_dir) if n.startswith("backups")] and "knowledge.db" in tree_listing(loc.db_dir)

    # and it recovers: the next build with a working normal builder makes a normal db from the new full db
    monkeypatch.setattr(normal_db.normal_db_service, "scan_against_full", real_scan)
    rebuild(loc)
    assert os.path.exists(normal)


def test_a_corrupt_privacy_rules_file_stops_the_build_before_publication_on_a_real_world(tmp_path, monkeypatch, capsys):
    """No patching and no injected step: the world's privacy_rules.json is corrupted after the first good build.
    Rules are read while the full db is built, so the build fails closed before anything is swapped in: the previous
    full and normal databases stay exactly as they were."""
    loc = make_locations(tmp_path, monkeypatch, ())
    rebuild(loc)
    normal = os.path.join(loc.db_dir, "knowledge-normal.db")
    full_before, normal_before, listing = digest(loc.db_path), digest(normal), tree_listing(loc.db_dir)
    with open(loc.privacy_rules(), "w") as f:
        f.write("{ this is not json")
    with pytest.raises(SystemExit) as exit_info:
        rebuild(loc)
    assert exit_info.value.code not in (0, None)
    assert "not valid JSON" in capsys.readouterr().err
    assert (digest(loc.db_path), digest(normal), tree_listing(loc.db_dir)) == (full_before, normal_before, listing)
