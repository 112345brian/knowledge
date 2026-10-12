"""The measurements load and the fact links through their ports (no file, no vault db, no knowledge db):
`measurements_service` against the foundation's in-memory repositories (tests/fake_build_repositories.py) and a
fake `vault_reader_port.VaultReader`, then the SQLite adapters (`vault_reader_store`, `measurements_store`)
against throwaway databases. The step-level behavior on a fixture vault is tests/test_measurements_dates.py,
tests/test_measurements_pins.py and the pipeline golden."""
import ast
import dataclasses
import inspect
import os
import sqlite3

import pytest

import build_records as r  # noqa: E402
from build_repositories_port import FactRepository, MeasurementRepository  # noqa: E402
from client import measurement_rules, measurements_service as svc, measurements_store, vault_reader_port, vault_reader_store  # noqa: E402
from client.vault_reader_port import ColumnTable, FoodLog, JefitSet, MealLog, WorkoutSet  # noqa: E402
from fake_build_repositories import (FakeFacts, FakeMeasurements, FakeSources, FakeState, FakeSubjects,  # noqa: E402
                                     FakeUnitOfWork)
from schema_helper import REPO, full_schema  # noqa: E402

SNAPSHOT = "2026-09-11"
MANUAL = {"dexa": "ck-dexa", "labcorp": "ck-lab", "tape": "ck-tape", "strength": "ck-str", "unlocated": "ck-unl"}
DEXA = {"total_mass_lbs": 160.0, "body_fat_pct": 18.0, "fat_mass_lbs": 29.0, "lean_tissue_lbs": 125.0, "bmc_lbs": 6.5,
        "bmd_total": 1.3, "bmd_z_score": 0.6, "rmr_kcal": 1900, "vat_mass_lbs": 0.9, "arms_lean_lbs": 22.0,
        "legs_lean_lbs": 42.0, "height_in": 70.0}


class FakeVault:
    """A VaultReader over plain lists; `calls` records the scan order the service asked for."""

    def __init__(self, **tables):
        self.t = tables
        self.calls = []
        self.closed = False

    def _get(self, name, default):
        self.calls.append(name)
        return self.t.get(name, default)

    def dexa_scan(self, scan_date):
        self.calls.append(f"dexa_scan:{scan_date}")
        return self.t.get("dexa", {}).get(scan_date)

    def bloodwork(self): return self._get("bloodwork", [])
    def body_measurements(self): return self._get("body_measurements", [])
    def latest_strength_checkpoint(self): return self._get("strength", None)
    def nutrition_daily(self): return self._get("nutrition_daily", [])
    def scale_readings(self): return self._get("scale_readings", [])
    def renpho_scale_readings(self): return self._get("renpho_scale_readings", [])
    def muscle_volume_weekly(self): return self._get("muscle_volume_weekly", ColumnTable((), []))
    def workout_sets(self): return self._get("workout_sets", [])
    def jefit_exercise_sets(self): return self._get("jefit_exercise_sets", [])
    def mfp_measurements(self): return self._get("mfp_measurements", [])
    def mfp_exercise_log(self): return self._get("mfp_exercise_log", [])
    def mfp_nutrition_log(self): return self._get("mfp_nutrition_log", [])
    def nutrition_food_log(self): return self._get("nutrition_food_log", [])
    def micronutrients(self): return self._get("micronutrients", ColumnTable((), []))
    def close(self): self.closed = True


class World:
    """The foundation's in-memory repositories over one state, with the manual sources step 01 seeds already in."""

    def __init__(self, sources=None):
        self.state = FakeState()
        self.measurements = FakeMeasurements(self.state)
        self.sources = FakeSources(self.state)
        self.subjects = FakeSubjects(self.state)
        self.unit_of_work = FakeUnitOfWork(self.state)
        for citekey in (MANUAL.values() if sources is None else sources):
            self.sources.add(r.NewSource(citekey=citekey, name=citekey, source_type="primary"))
        self.state.operations.clear()

    def deps(self, vault):
        return svc.MeasurementsDeps(vault=vault, measurements=self.measurements, sources=self.sources,
                                    subjects=self.subjects, unit_of_work=self.unit_of_work)

    @property
    def ops(self):
        return self.state.operations

    def metric(self, key):
        return self.state.metrics[self.measurements.find_metric(key)]

    def source_id(self, citekey):
        return self.sources.find(citekey)


def load(vault=None, world=None, scans=(("2026-01-01", "dexa"),), sources=None):
    vault = vault if vault is not None else FakeVault(dexa={"2026-01-01": DEXA})
    world = world if world is not None else World()
    summary = svc.load_measurements(world.deps(vault), snapshot_date=SNAPSHOT, notes_vault="/vault",
                                    manual_sources=sources or MANUAL, scans=list(scans))
    return summary, vault, world


# ---------------------------------------------------------------- the load, against fakes

def test_a_scan_becomes_the_rules_readings_with_ids_resolved_and_the_snapshot_date_stamped():
    _, _, w = load()
    expected = measurement_rules.dexa_measurements(DEXA, "2026-01-01", "dexa")
    got = w.state.measurements
    assert [(m.measured_at, m.value, m.trust_level, m.notes) for m in got] == \
           [(m.measured_at, m.value, m.trust_level, m.notes) for m in expected]
    assert {m.date_added for m in got} == {SNAPSHOT}
    assert {m.source_id for m in got} == {w.source_id("ck-dexa")}
    assert w.measurements.find_metric("body_fat_pct") == got[1].metric_id
    fat, lmi = w.metric("body_fat_pct"), w.metric("lmi")
    assert (fat.unit, fat.label, fat.good_direction) == ("%", "Body fat", -1)      # the curated label and direction
    assert (lmi.label, lmi.good_direction) == ("LMI", None)
    assert w.state.commits == 1


def test_a_metric_without_curated_metadata_gets_the_generated_label():
    vault = FakeVault(dexa={"2026-01-01": DEXA}, micronutrients=ColumnTable(
        ("vitamin_d_iu",), [{"date": "2026-02-05", "vitamin_d_iu": 400}]))
    _, _, w = load(vault)
    m = w.metric("vitamin_d_iu")
    assert (m.unit, m.label, m.good_direction) == ("iu", "Vitamin d iu", None)


class CountingMeasurements(FakeMeasurements):
    def __init__(self, state):
        super().__init__(state)
        self.metric_lookups = []

    def find_metric(self, key):
        self.metric_lookups.append(key)
        return super().find_metric(key)


def test_the_per_run_caches_dedupe_lookups_within_a_run_and_do_not_survive_it():
    tape = {c: None for c in measurement_rules.TAPE_COLUMNS if c != "waist_in"}
    vault = FakeVault(dexa={"2026-01-01": DEXA}, body_measurements=[
        {"date": "2026-01-01", "waist_in": 32, **tape}, {"date": "2026-01-08", "waist_in": 31.5, **tape}])
    w = World()
    w.measurements = counting = CountingMeasurements(w.state)
    load(vault, w)
    assert counting.metric_lookups.count("waist_in") == 1                          # two readings, one lookup
    assert w.ops.count(("subjects.add", "body-composition")) == 1
    n = len(counting.metric_lookups)
    load(vault, w)                                                                 # a second run on the same repositories
    assert len(counting.metric_lookups) == 2 * n                                   # asks again: nothing was kept between runs
    assert w.ops.count(("subjects.add", "body-composition")) == 1                  # and found what the first run made
    assert [op[1] for op in w.ops if op[0] == "measurements.add_metric"] == [m.key for m in w.state.metrics.values()]


def test_sources_are_created_in_the_original_order_between_the_readings():
    vault = FakeVault(
        dexa={"2026-01-01": DEXA},
        strength={"date": "2026-03-01", "squat_lbs": 310, "squat_bw_ratio": 1.6, "squat_as_of": "2026-02-28",
                  "bench_lbs": None, "bench_bw_ratio": None, "bench_as_of": None,
                  "deadlift_lbs": None, "deadlift_bw_ratio": None, "deadlift_as_of": None, "note": None},
        nutrition_daily=[{"date": "2026-02-01", "weight_lbs": 161.0, "trend_weight_lbs": None, "calories_kcal": None,
                          "protein_g": None, "fat_g": None, "carbs_g": None, "expenditure_kcal": None}],
        muscle_volume_weekly=ColumnTable(("chest_sets",), [{"date": "2026-02-02", "chest_sets": 10}]),
        workout_sets=[WorkoutSet("2026-02-03", "Squat", 225, "lb", 5, 2, 0, None)])
    _, _, w = load(vault)
    order = []
    kinds = {"measurements.add": "measurement", "measurements.add_muscle_volume": "volume",
             "measurements.add_training_set": "training_set"}
    for op in w.ops:
        if op[0] == "sources.add":
            order.append(op[1])
        elif op[0] in kinds and (not order or order[-1] != kinds[op[0]]):
            order.append(kinds[op[0]])
    # dexa + strength readings, then the four summary sources, nutrition reading, volume rows, then the raw sources
    assert order == ["measurement", "vault-db-nutrition-daily", "vault-db-scale-readings", "vault-db-scale-estimates",
                     "vault-db-muscle-volume-weekly", "measurement", "volume", "vault-db-workout-sets",
                     "vault-db-jefit-sets", "vault-db-mfp-measurements", "vault-db-mfp-exercise-log",
                     "vault-db-mfp-nutrition-log", "vault-db-nutrition-food-log", "vault-db-micronutrients",
                     "training_set"]
    created = {s.citekey: s for s in w.state.sources.values() if s.citekey.startswith("vault-db-")}
    daily = created["vault-db-nutrition-daily"]
    assert (daily.retrieved_date, daily.source_type) == (SNAPSHOT, "primary")
    assert daily.origin_path == "/vault/source.db#nutrition_daily"
    assert f"as of {SNAPSHOT}" in daily.description
    assert set(w.state.authors) == {"user"} and len(w.state.source_authors) == len(created)


def test_a_vault_source_that_is_already_stored_is_reused_not_recreated():
    w = World()
    load(world=w)
    stored = dict(w.sources.citekeys())
    links = len(w.state.source_authors)
    load(world=w)
    assert dict(w.sources.citekeys()) == stored and len(w.state.source_authors) == links


def test_event_tables_apply_the_fallbacks_and_flags():
    vault = FakeVault(
        dexa={"2026-01-01": DEXA},
        workout_sets=[WorkoutSet("2026-02-03", None, 100, None, 8, None, 1, "warm"),
                      WorkoutSet("2026-02-03", "Squat", 225, "kg", 5, 2, 0, None)],
        jefit_exercise_sets=[JefitSet("2020-01-01", None, 135, "kg", 5)],
        nutrition_food_log=[FoodLog("2026-02-05", "12:00", None, 1, "cup", 200, 1, 45, 4, 0)])
    summary, _, w = load(vault)
    sets = w.state.training_sets
    assert [(s.weight_unit, s.is_warmup, s.rir, s.notes) for s in sets] == [("lb", 1, None, "warm"), ("kg", 0, 2, None),
                                                                           ("kg", 0, None, None)]
    assert w.state.exercises == {"Unknown exercise": 1, "Squat": 2}
    assert w.state.foods == {"Unknown food": 1}
    assert (summary.exercises, summary.foods, summary.training_sets, summary.food_log_entries) == (2, 1, 3, 1)


def test_a_meal_log_row_carries_every_nutrient_by_column_name():
    values = {c: float(i) for i, c in enumerate(svc.MEAL_LOG_NUTRIENTS, 1)}
    vault = FakeVault(dexa={"2026-01-01": DEXA}, mfp_nutrition_log=[MealLog("2026-02-04", "Lunch", **values)])
    _, _, w = load(vault)
    [entry] = w.state.meal_log
    assert (entry.measured_at, entry.meal, dict(entry.nutrients)) == ("2026-02-04", "Lunch", values)
    assert entry.source_id == w.source_id("vault-db-mfp-nutrition-log")
    assert [f.name for f in dataclasses.fields(MealLog)] == ["date", "meal", *svc.MEAL_LOG_NUTRIENTS]
    assert measurements_store.MEAL_LOG_COLUMNS == svc.MEAL_LOG_NUTRIENTS


def test_nulls_in_a_dynamic_column_table_are_skipped_and_an_empty_table_makes_nothing():
    vault = FakeVault(dexa={"2026-01-01": DEXA}, muscle_volume_weekly=ColumnTable(
        ("chest_sets", "upper_back_sets"), [{"date": "2026-02-02", "chest_sets": 10, "upper_back_sets": None}]),
        micronutrients=ColumnTable(("iron_mg", "zinc_mg"), [{"date": "2026-02-05", "iron_mg": None, "zinc_mg": 9}]))
    _, _, w = load(vault)
    assert w.state.muscles == {"chest": 1}
    assert [v.sets for v in w.state.muscle_volume] == [10]
    assert [m.key for m in w.state.metrics.values() if m.key.endswith("_mg")] == ["zinc_mg"]
    _, _, empty = load(FakeVault(dexa={"2026-01-01": DEXA}, muscle_volume_weekly=ColumnTable(("chest_sets",), [])))
    assert empty.state.muscles == {} and empty.state.muscle_volume == []


def test_a_missing_manual_source_names_the_citekey_and_writes_nothing():
    w = World(sources=["ck-dexa"])
    with pytest.raises(RuntimeError, match="ck-lab.*seed_sources"):
        load(world=w)
    assert w.ops == [] and w.state.commits == 0


def test_a_configured_scan_the_vault_lacks_fails_naming_its_date():
    with pytest.raises(RuntimeError, match="2027-05-05"):
        load(scans=(("2026-01-01", "dexa"), ("2027-05-05", "dexa")))


def test_a_reading_for_a_source_key_the_config_did_not_name_is_an_error_not_a_guess():
    bloodwork = [{"date": "2026-01-03", "analyte": "LH", "value": 3.1, "unit": "mIU/mL", "ref_low": None, "ref_high": None,
                  "flag": None, "notes": "SOURCE DOCUMENT NOT FOUND", "on_medication": None}]
    with pytest.raises(KeyError, match="unlocated"):
        load(FakeVault(dexa={"2026-01-01": DEXA}, bloodwork=bloodwork), sources={k: v for k, v in MANUAL.items() if k != "unlocated"})


def test_the_summary_counts_this_run_and_matches_the_report_lines():
    w = World()
    for _ in range(100):                        # rows an earlier load left: `inserted` is this run's only
        w.measurements.add(r.NewMeasurement(r.SubjectId(1), r.MetricId(1), 1, "2020-01-01", r.SourceId(1), "high",
                                            None, "2020-01-01"))
    summary, _, _ = load(world=w)
    n = len(measurement_rules.dexa_measurements(DEXA, "2026-01-01", "dexa"))
    assert summary.inserted == len(w.state.measurements) - 100 == n
    lines = measurement_rules.summary_lines(summary)
    assert lines[0] == f"[measurements] inserted {n} measurement rows (3 subjects, {n} metrics touched)"
    assert lines[1].startswith("  plus 0 training_sets (0 exercises), 0 food_log_entries (0 foods), 0 meal_log_entries, 0 muscle_volume_weekly rows (0 muscles)")


# ---------------------------------------------------------------- fact links

class FakeLinks:
    def __init__(self, facts, per_metric=3):
        self.facts, self.per_metric, self.linked = facts, per_metric, []

    def find_by_source_key(self, source_key): return self.facts.get(source_key)
    def link_measurements(self, fact_id, metric_key): self.linked.append((fact_id, metric_key)); return self.per_metric


def link(links, items):
    state = FakeState()
    linked = svc.link_fact_measurements(svc.FactLinksDeps(facts=links, unit_of_work=FakeUnitOfWork(state)), items)
    return linked, state.commits


def test_facts_are_linked_to_the_metric_they_cite_and_invalid_or_unloaded_ones_are_skipped():
    links = FakeLinks({"a": 1, "b": 2})
    items = [{"_source_key": "a", "measurement_metric_link": "weight_lb"},
             {"_source_key": "b"},                                                     # cites nothing
             {"_source_key": "x", "measurement_metric_link": "weight_lb"},             # the facts step skipped it
             {"_source_key": "b", "measurement_metric_link": ""},
             {"_source_key": "b", "measurement_metric_link": "waist_in"}]
    assert link(links, items) == (6, 1)
    assert links.linked == [(1, "weight_lb"), (2, "waist_in")]


def test_no_items_commits_once_and_links_nothing():
    assert link(FakeLinks({}), []) == (0, 1)


def test_the_foundation_fake_of_the_fact_repository_answers_the_link_step():
    """The two members the step calls are `FactRepository`'s own: its in-memory fake links the way the step expects."""
    state = FakeState()
    measurements = FakeMeasurements(state)
    metric = measurements.add_metric(r.NewMetric("weight_lb", "Total weight", "lb", 1))
    for day in ("2026-01-01", "2026-01-02"):
        measurements.add(r.NewMeasurement(r.SubjectId(1), metric, 1.0, day, r.SourceId(1), "high", None, SNAPSHOT))
    state.facts[1] = type("F", (), {"source_key": "p-1"})()
    linked, commits = link(FakeFacts(state), [{"_source_key": "p-1", "measurement_metric_link": "weight_lb"},
                                              {"_source_key": "p-9", "measurement_metric_link": "weight_lb"}])
    assert (linked, commits) == (2, 1)
    assert state.fact_measurements == [(1, "weight_lb"), (1, "weight_lb")]


# ---------------------------------------------------------------- guards

def test_the_service_holds_no_module_state_and_imports_no_adapter():
    tree = ast.parse(open(os.path.join(REPO, "client", "measurements_service.py"), encoding="utf-8").read())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"sqlite3", "os", "client.measurements_store", "client.vault_reader_store"}, imported
    mutable = [t.id for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, (ast.Dict, ast.List, ast.Set))
               for t in n.targets if isinstance(t, ast.Name)]
    assert mutable == []


def test_every_driver_of_the_load_uses_the_summary_it_returns():
    """`load_measurements` returns what was written; a caller that drops it would print nothing and hide an empty load."""
    for name in ("measurements.py", "link_fact_measurements.py"):
        tree = ast.parse(open(os.path.join(REPO, "client", name), encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute):
                assert n.value.func.attr not in ("load_measurements", "link_fact_measurements"), f"{name}:{n.lineno} discards the result"


def test_the_lane_port_is_gone_and_no_module_defines_a_second_measurement_repository():
    assert not os.path.exists(os.path.join(REPO, "client", "measurements_port.py"))
    for name in sorted(os.listdir(os.path.join(REPO, "client"))):
        if name.endswith(".py"):
            text = open(os.path.join(REPO, "client", name), encoding="utf-8").read()
            assert "class MeasurementRepository" not in text and "class FactMeasurementLinks" not in text, name


# ---------------------------------------------------------------- the SQLite adapters

VAULT_SCHEMA = {
    "bodycomp_dexa": "date, total_mass_lbs",
    "bloodwork": "date, analyte, value",
    "strength_checkpoints": "date, squat_lbs",
    "muscle_volume_weekly": "date, chest_sets, upper_back_sets",
    "micronutrients": "date, iron_mg, vitamin_d_iu, source_file",
    "workout_sets": "date, exercise, completed_weight, weight_unit, completed_reps, rir, is_warmup, notes",
}


@pytest.fixture
def vault_db():
    con = sqlite3.connect(":memory:")
    for table, cols in VAULT_SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
    return con


def test_scans_come_back_in_the_tables_own_order_as_plain_records(vault_db):
    for d in ("2026-03-01", "2025-01-01", "2026-01-01"):                          # deliberately not date order
        vault_db.execute("INSERT INTO bloodwork VALUES (?, 'LH', 1.0)", (d,))
    vault_db.execute("INSERT INTO workout_sets VALUES ('2026-02-03', 'Squat', 225, 'lb', 5, 2, 0, 'n')")
    reader = vault_reader_store.SqliteVaultReader(vault_db)
    assert [row["date"] for row in reader.bloodwork()] == ["2026-03-01", "2025-01-01", "2026-01-01"]
    assert all(type(row) is dict for row in reader.bloodwork())
    assert list(reader.workout_sets()) == [WorkoutSet("2026-02-03", "Squat", 225, "lb", 5, 2, 0, "n")]


def test_one_scan_and_the_latest_checkpoint(vault_db):
    vault_db.executemany("INSERT INTO bodycomp_dexa VALUES (?, ?)", [("2025-01-01", 150.0), ("2026-01-01", 160.0)])
    vault_db.executemany("INSERT INTO strength_checkpoints VALUES (?, ?)", [("2026-01-01", 300), ("2026-03-01", 310)])
    reader = vault_reader_store.SqliteVaultReader(vault_db)
    assert reader.dexa_scan("2026-01-01") == {"date": "2026-01-01", "total_mass_lbs": 160.0}
    assert reader.dexa_scan("1999-01-01") is None
    assert reader.latest_strength_checkpoint() == {"date": "2026-03-01", "squat_lbs": 310}
    vault_db.execute("DELETE FROM strength_checkpoints")
    assert reader.latest_strength_checkpoint() is None


def test_dynamic_metric_columns_come_from_the_table_definition_even_when_it_is_empty(vault_db):
    reader = vault_reader_store.SqliteVaultReader(vault_db)
    assert reader.micronutrients().columns == ("iron_mg", "vitamin_d_iu")          # not date, not source_file
    assert reader.muscle_volume_weekly().columns == ("chest_sets", "upper_back_sets")
    assert list(reader.micronutrients().rows) == []
    vault_db.execute("ALTER TABLE micronutrients ADD COLUMN zinc_mg")               # a new nutrient appears in the vault
    vault_db.execute("INSERT INTO micronutrients VALUES ('2026-02-05', 1.0, NULL, 'f.csv', 9.0)")
    table = reader.micronutrients()
    assert table.columns == ("iron_mg", "vitamin_d_iu", "zinc_mg")
    assert [dict(row) for row in table.rows] == [{"date": "2026-02-05", "iron_mg": 1.0, "vitamin_d_iu": None,
                                                  "source_file": "f.csv", "zinc_mg": 9.0}]


@pytest.fixture
def knowledge_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    con.executescript(full_schema())
    return con


def _seed(con):
    source = con.execute("INSERT INTO sources (citekey, name, source_type) VALUES ('s', 'n', 'primary')").lastrowid
    subject = con.execute("INSERT INTO subjects (name) VALUES ('body-composition')").lastrowid
    return r.SourceId(source), r.SubjectId(subject)


def test_the_repository_gets_or_creates_dimensions_and_finds_metrics_without_rewriting_them(knowledge_db):
    repo = measurements_store.SqliteMeasurementRepository(knowledge_db)
    for method in ("exercise", "food", "muscle"):
        first = getattr(repo, method)("thing")
        assert getattr(repo, method)("thing") == first
        assert getattr(repo, method)("other") == first + 1
    assert repo.find_metric("body_fat_pct") is None
    m = repo.add_metric(r.NewMetric("body_fat_pct", "Body fat", "%", -1))
    assert repo.find_metric("body_fat_pct") == m
    assert tuple(knowledge_db.execute("SELECT label, unit, good_direction FROM metrics WHERE id = ?", (m,)).fetchone()) == ("Body fat", "%", -1)
    assert repo.add_metric(r.NewMetric("plain", "Plain", "u", None)) == m + 1
    assert knowledge_db.execute("SELECT good_direction FROM metrics WHERE key = 'plain'").fetchone()[0] is None


def test_the_repository_writes_each_record_kind_and_counts_whole_tables(knowledge_db):
    repo = measurements_store.SqliteMeasurementRepository(knowledge_db)
    src, subject = _seed(knowledge_db)
    metric = repo.add_metric(r.NewMetric("k", "K", "u", None))
    repo.add(r.NewMeasurement(subject, metric, 1.5, "2026-01-01", src, "high", "why", SNAPSHOT, "n", "soon", "r"))
    repo.add_training_set(r.NewTrainingSet(repo.exercise("Squat"), "2026-01-01", 225, "lb", 5, None, 1, src, "x"))
    repo.add_muscle_volume(r.NewMuscleVolume(repo.muscle("chest"), "2026-01-05", 10, src))
    nutrients = {c: float(i) for i, c in enumerate(measurements_store.MEAL_LOG_COLUMNS, 1)}
    repo.add_meal_log_entry(r.NewMealLogEntry("2026-01-01", "Lunch", nutrients, src))
    repo.add_food_log_entry(r.NewFoodLogEntry(repo.food("Rice"), "2026-01-01", "12:00", 1, "cup", 200, 1, 45, 4, 0, src))
    assert repo.totals() == r.MeasurementTotals(1, 1, 1, 1, 1)
    assert tuple(knowledge_db.execute("SELECT subject_id, metric_id, value, date_added, notes, recheck_by, recheck_rationale "
                                      "FROM measurements").fetchone()) == (subject, metric, 1.5, SNAPSHOT, "n", "soon", "r")
    assert tuple(knowledge_db.execute("SELECT is_warmup, rir FROM training_sets").fetchone()) == (1, None)
    meal = dict(knowledge_db.execute("SELECT * FROM meal_log_entries").fetchone())
    assert {k: meal[k] for k in ("measured_at", "meal", "source_id", *nutrients)} == {
        "measured_at": "2026-01-01", "meal": "Lunch", "source_id": src, **nutrients}


def test_the_repository_never_commits(knowledge_db):
    repo = measurements_store.SqliteMeasurementRepository(knowledge_db)
    repo.exercise("Squat")
    knowledge_db.rollback()
    assert knowledge_db.execute("SELECT COUNT(*) FROM exercises").fetchone()[0] == 0


def test_links_attach_every_measurement_of_the_metric_to_the_fact(knowledge_db):
    repo = measurements_store.SqliteMeasurementRepository(knowledge_db)
    src, subject = _seed(knowledge_db)
    for key, n in (("weight_lb", 2), ("waist_in", 1)):
        metric = repo.add_metric(r.NewMetric(key, key, "u", None))
        for i in range(n):
            repo.add(r.NewMeasurement(subject, metric, 1.0, f"2026-01-0{i + 1}", src, "high", None, SNAPSHOT))
    knowledge_db.execute("INSERT INTO facts (statement, subject_id, source_key, trust_level, freshness) "
                         "VALUES ('f', ?, 'p-2', 'high', 'unreviewed')", (subject,))
    links = measurements_store.SqliteFactMeasurementLinks(knowledge_db)
    fact_id = links.find_by_source_key("p-2")
    assert fact_id is not None and links.find_by_source_key("nope") is None
    assert links.link_measurements(fact_id, "weight_lb") == 2
    assert links.link_measurements(fact_id, "no_such_metric") == 0
    assert knowledge_db.execute("SELECT COUNT(*) FROM fact_measurements WHERE fact_id = ?", (fact_id,)).fetchone()[0] == 2


def test_each_adapter_has_every_member_of_its_port_with_the_same_parameters():
    """The runtime half of what tests/port_types.py checks for mypy: names and parameter order."""
    links = measurements_store.SqliteFactMeasurementLinks
    pairs = [(vault_reader_port.VaultReader, vault_reader_store.SqliteVaultReader, None),
             (MeasurementRepository, measurements_store.SqliteMeasurementRepository, None),
             (FactRepository, links, {"find_by_source_key", "link_measurements"}),
             (svc.FactLinks, links, None)]
    for proto, impl, only in pairs:
        for name, member in inspect.getmembers(proto, inspect.isfunction):
            if name.startswith("_") or (only is not None and name not in only):
                continue
            assert hasattr(impl, name), f"{impl.__name__} lacks {name}"
            want = list(inspect.signature(member).parameters)
            assert list(inspect.signature(getattr(impl, name)).parameters) == want, f"{impl.__name__}.{name}"
