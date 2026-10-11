"""The measurements load and the fact links through their ports (no file, no vault db, no knowledge db):
`measurements_service` against in-memory fakes of `vault_reader_port.VaultReader` and
`measurements_port.MeasurementRepository`, then the SQLite adapters (`vault_reader_store`, `measurements_store`)
against throwaway databases. The step-level behavior on a fixture vault is tests/test_measurements_dates.py and
the pipeline golden."""
import ast
import os
import sqlite3

import pytest

from client import measurement_rules, measurements_service as svc, measurements_store, vault_reader_store  # noqa: E402
from client.measurements_port import TableCounts  # noqa: E402
from client.vault_reader_port import ColumnTable, FoodLog, JefitSet, MealLog, WorkoutSet  # noqa: E402
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


class FakeRepository:
    """A MeasurementRepository in memory. `events` is the ordered write log (kind, payload)."""

    def __init__(self, sources=None):
        self.sources = dict(sources if sources is not None else {ck: n + 1 for n, ck in enumerate(MANUAL.values())})
        self.dims = {k: {} for k in ("subject", "metric", "exercise", "food", "muscle")}
        self.lookups = []
        self.events = []
        self.committed = 0
        self.rows = {k: 0 for k in ("measurement", "training", "volume", "meal", "food")}

    def source_id(self, citekey):
        return self.sources.get(citekey)

    def get_or_create_source(self, citekey, name, description, origin_path, retrieved_date, author):
        self.events.append(("source", (citekey, retrieved_date, author, origin_path, description)))
        return self.sources.setdefault(citekey, len(self.sources) + 1)

    def _dim(self, kind, key):
        self.lookups.append((kind, key))
        return self.dims[kind].setdefault(key, len(self.dims[kind]) + 1)

    def get_or_create_subject(self, name): return self._dim("subject", name)
    def get_or_create_exercise(self, name): return self._dim("exercise", name)
    def get_or_create_food(self, name): return self._dim("food", name)
    def get_or_create_muscle(self, name): return self._dim("muscle", name)

    def get_or_create_metric(self, key, unit, label, good_direction):
        self.metric_args = getattr(self, "metric_args", {})
        self.metric_args.setdefault(key, (unit, label, good_direction))
        return self._dim("metric", key)

    def add_measurement(self, r): self.rows["measurement"] += 1; self.events.append(("measurement", r))
    def add_training_set(self, r): self.rows["training"] += 1; self.events.append(("training_set", r))
    def add_muscle_volume(self, r): self.rows["volume"] += 1; self.events.append(("volume", r))
    def add_meal_log_entry(self, r): self.rows["meal"] += 1; self.events.append(("meal", r))
    def add_food_log_entry(self, r): self.rows["food"] += 1; self.events.append(("food", r))

    def counts(self):
        return TableCounts(self.rows["measurement"], self.rows["training"], self.rows["food"], self.rows["meal"],
                           self.rows["volume"])

    def commit(self): self.committed += 1

    def of(self, kind):
        return [p for k, p in self.events if k == kind]


def load(vault=None, repo=None, scans=(("2026-01-01", "dexa"),), sources=None):
    vault = vault if vault is not None else FakeVault(dexa={"2026-01-01": DEXA})
    repo = repo if repo is not None else FakeRepository()
    summary = svc.load_measurements(svc.MeasurementsDeps(vault=vault, repository=repo), snapshot_date=SNAPSHOT,
                                    notes_vault="/vault", manual_sources=sources or MANUAL, scans=list(scans))
    return summary, vault, repo


# ---------------------------------------------------------------- the load, against fakes

def test_a_scan_becomes_the_rules_readings_with_ids_resolved_and_the_snapshot_date_stamped():
    _, _, repo = load()
    expected = measurement_rules.dexa_measurements(DEXA, "2026-01-01", "dexa")
    got = repo.of("measurement")
    assert [(r.measured_at, r.value, r.trust_level, r.notes) for r in got] == \
           [(m.measured_at, m.value, m.trust_level, m.notes) for m in expected]
    assert {r.date_added for r in got} == {SNAPSHOT}
    assert {r.source_id for r in got} == {repo.sources["ck-dexa"]}
    assert repo.dims["metric"]["body_fat_pct"] == got[1].metric_id
    assert repo.metric_args["body_fat_pct"] == ("%", "Body fat", -1)             # the curated label and direction
    assert repo.metric_args["lmi"][1:] == ("LMI", None)
    assert repo.committed == 1


def test_a_metric_without_curated_metadata_gets_the_generated_label():
    vault = FakeVault(dexa={"2026-01-01": DEXA}, micronutrients=ColumnTable(
        ("vitamin_d_iu",), [{"date": "2026-02-05", "vitamin_d_iu": 400}]))
    _, _, repo = load(vault)
    assert repo.metric_args["vitamin_d_iu"] == ("iu", "Vitamin d iu", None)


def test_the_per_run_caches_dedupe_lookups_within_a_run_and_do_not_survive_it():
    vault = FakeVault(dexa={"2026-01-01": DEXA}, body_measurements=[
        {"date": "2026-01-01", "waist_in": 32, **{c: None for c in measurement_rules.TAPE_COLUMNS if c != "waist_in"}},
        {"date": "2026-01-08", "waist_in": 31.5, **{c: None for c in measurement_rules.TAPE_COLUMNS if c != "waist_in"}}])
    _, _, repo = load(vault)
    assert repo.lookups.count(("metric", "waist_in")) == 1                         # two readings, one lookup
    assert repo.lookups.count(("subject", "body-composition")) == 1
    n = len(repo.lookups)
    load(vault, repo)                                                              # a second run on the same repository
    assert len(repo.lookups) == 2 * n                                              # asks again: nothing was kept between runs


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
    _, _, repo = load(vault)
    order = []
    for kind, payload in repo.events:
        if kind == "source":
            order.append(payload[0])
        elif kind in ("measurement", "volume", "training_set") and (not order or order[-1] != kind):
            order.append(kind)
    # dexa + strength readings, then the four summary sources, nutrition reading, volume rows, then the raw sources
    assert order == ["measurement", "vault-db-nutrition-daily", "vault-db-scale-readings", "vault-db-scale-estimates",
                     "vault-db-muscle-volume-weekly", "measurement", "volume", "vault-db-workout-sets",
                     "vault-db-jefit-sets", "vault-db-mfp-measurements", "vault-db-mfp-exercise-log",
                     "vault-db-mfp-nutrition-log", "vault-db-nutrition-food-log", "vault-db-micronutrients",
                     "training_set"]
    created = {p[0]: p for k, p in repo.events if k == "source"}
    assert created["vault-db-nutrition-daily"][1:3] == (SNAPSHOT, "user")
    assert created["vault-db-nutrition-daily"][3] == "/vault/source.db#nutrition_daily"
    assert f"as of {SNAPSHOT}" in created["vault-db-nutrition-daily"][4]


def test_event_tables_apply_the_fallbacks_and_flags():
    vault = FakeVault(
        dexa={"2026-01-01": DEXA},
        workout_sets=[WorkoutSet("2026-02-03", None, 100, None, 8, None, 1, "warm"),
                      WorkoutSet("2026-02-03", "Squat", 225, "kg", 5, 2, 0, None)],
        jefit_exercise_sets=[JefitSet("2020-01-01", None, 135, "kg", 5)],
        nutrition_food_log=[FoodLog("2026-02-05", "12:00", None, 1, "cup", 200, 1, 45, 4, 0)])
    summary, _, repo = load(vault)
    sets = repo.of("training_set")
    assert [(s.weight_unit, s.is_warmup, s.rir, s.notes) for s in sets] == [("lb", 1, None, "warm"), ("kg", 0, 2, None),
                                                                           ("kg", 0, None, None)]
    assert repo.dims["exercise"] == {"Unknown exercise": 1, "Squat": 2}
    assert repo.dims["food"] == {"Unknown food": 1}
    assert (summary.exercises, summary.foods, summary.training_sets, summary.food_log_entries) == (2, 1, 3, 1)


def test_nulls_in_a_dynamic_column_table_are_skipped_and_an_empty_table_makes_nothing():
    vault = FakeVault(dexa={"2026-01-01": DEXA}, muscle_volume_weekly=ColumnTable(
        ("chest_sets", "upper_back_sets"), [{"date": "2026-02-02", "chest_sets": 10, "upper_back_sets": None}]),
        micronutrients=ColumnTable(("iron_mg", "zinc_mg"), [{"date": "2026-02-05", "iron_mg": None, "zinc_mg": 9}]))
    _, _, repo = load(vault)
    assert repo.dims["muscle"] == {"chest": 1}
    assert [v.sets for v in repo.of("volume")] == [10]
    assert [r for r in repo.dims["metric"] if r.endswith("_mg")] == ["zinc_mg"]
    _, _, empty = load(FakeVault(dexa={"2026-01-01": DEXA}, muscle_volume_weekly=ColumnTable(("chest_sets",), [])))
    assert empty.dims["muscle"] == {} and empty.of("volume") == []


def test_a_missing_manual_source_names_the_citekey_and_writes_nothing():
    repo = FakeRepository(sources={"ck-dexa": 1})
    with pytest.raises(RuntimeError, match="ck-lab.*seed_sources"):
        load(repo=repo)
    assert repo.events == [] and repo.committed == 0


def test_a_configured_scan_the_vault_lacks_fails_naming_its_date():
    with pytest.raises(RuntimeError, match="2027-05-05"):
        load(scans=(("2026-01-01", "dexa"), ("2027-05-05", "dexa")))


def test_a_reading_for_a_source_key_the_config_did_not_name_is_an_error_not_a_guess():
    bloodwork = [{"date": "2026-01-03", "analyte": "LH", "value": 3.1, "unit": "mIU/mL", "ref_low": None, "ref_high": None,
                  "flag": None, "notes": "SOURCE DOCUMENT NOT FOUND", "on_medication": None}]
    with pytest.raises(KeyError, match="unlocated"):
        load(FakeVault(dexa={"2026-01-01": DEXA}, bloodwork=bloodwork), sources={k: v for k, v in MANUAL.items() if k != "unlocated"})


def test_the_summary_counts_this_run_and_matches_the_report_lines():
    repo = FakeRepository()
    repo.rows["measurement"] = 100          # rows an earlier load left: `inserted` is this run's only
    summary, _, _ = load(repo=repo)
    n = len(measurement_rules.dexa_measurements(DEXA, "2026-01-01", "dexa"))
    assert summary.inserted == repo.rows["measurement"] - 100 == n
    lines = measurement_rules.summary_lines(summary)
    assert lines[0] == f"[measurements] inserted {n} measurement rows (3 subjects, {n} metrics touched)"
    assert lines[1].startswith("  plus 0 training_sets (0 exercises), 0 food_log_entries (0 foods), 0 meal_log_entries, 0 muscle_volume_weekly rows (0 muscles)")


# ---------------------------------------------------------------- fact links

class FakeLinks:
    def __init__(self, facts, per_metric=3):
        self.facts, self.per_metric, self.linked, self.committed = facts, per_metric, [], 0

    def fact_id(self, source_key): return self.facts.get(source_key)
    def link_metric(self, fact_id, metric_key): self.linked.append((fact_id, metric_key)); return self.per_metric
    def commit(self): self.committed += 1


def test_facts_are_linked_to_the_metric_they_cite_and_invalid_or_unloaded_ones_are_skipped():
    links = FakeLinks({"a": 1, "b": 2})
    items = [{"_source_key": "a", "measurement_metric_link": "weight_lb"},
             {"_source_key": "b"},                                                     # cites nothing
             {"_source_key": "x", "measurement_metric_link": "weight_lb"},             # the facts step skipped it
             {"_source_key": "b", "measurement_metric_link": ""},
             {"_source_key": "b", "measurement_metric_link": "waist_in"}]
    assert svc.link_facts(svc.FactLinksDeps(links=links), items) == 6
    assert links.linked == [(1, "weight_lb"), (2, "waist_in")]
    assert links.committed == 1


def test_no_items_commits_once_and_links_nothing():
    links = FakeLinks({})
    assert svc.link_facts(svc.FactLinksDeps(links=links), []) == 0
    assert links.committed == 1


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
                assert n.value.func.attr not in ("load_measurements", "link_facts"), f"{name}:{n.lineno} discards the result"


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
    assert [r["date"] for r in reader.bloodwork()] == ["2026-03-01", "2025-01-01", "2026-01-01"]
    assert all(type(r) is dict for r in reader.bloodwork())
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
    assert [dict(r) for r in table.rows] == [{"date": "2026-02-05", "iron_mg": 1.0, "vitamin_d_iu": None,
                                               "source_file": "f.csv", "zinc_mg": 9.0}]


@pytest.fixture
def knowledge_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    con.executescript(full_schema())
    return con


def test_the_repository_looks_up_before_it_inserts_and_links_source_authors(knowledge_db):
    repo = measurements_store.SqliteMeasurementRepository(knowledge_db)
    assert repo.source_id("vault-x") is None
    sid = repo.get_or_create_source("vault-x", "Name", "Desc", "/v/source.db#t", SNAPSHOT, "user")
    assert repo.get_or_create_source("vault-x", "Other", "Other", None, "2030-01-01", "user") == sid
    assert repo.source_id("vault-x") == sid
    row = knowledge_db.execute("SELECT source_type, retrieved_date, description, origin_path FROM sources WHERE id = ?", (sid,)).fetchone()
    assert tuple(row) == ("primary", SNAPSHOT, "Desc", "/v/source.db#t")
    repo.get_or_create_source("vault-y", "N2", "D2", None, SNAPSHOT, "user")
    assert knowledge_db.execute("SELECT COUNT(*) FROM authors WHERE name = 'user'").fetchone()[0] == 1
    assert knowledge_db.execute("SELECT COUNT(*) FROM source_authors").fetchone()[0] == 2
    for method in ("subject", "exercise", "food", "muscle"):
        a = getattr(repo, f"get_or_create_{method}")("thing")
        assert getattr(repo, f"get_or_create_{method}")("thing") == a
    m = repo.get_or_create_metric("body_fat_pct", "%", "Body fat", -1)
    assert repo.get_or_create_metric("body_fat_pct", "kg", "Changed", 1) == m       # an existing metric is not rewritten
    assert tuple(knowledge_db.execute("SELECT label, unit, good_direction FROM metrics WHERE id = ?", (m,)).fetchone()) == ("Body fat", "%", -1)


def test_the_repository_writes_each_record_kind_and_counts_whole_tables(knowledge_db):
    from client.measurements_port import (FoodLogRecord, MealLogRecord, MeasurementRecord, MuscleVolumeRecord,
                                          TrainingSetRecord)
    repo = measurements_store.SqliteMeasurementRepository(knowledge_db)
    src = repo.get_or_create_source("s", "n", "d", None, SNAPSHOT, "user")
    subject, metric = repo.get_or_create_subject("body-composition"), repo.get_or_create_metric("k", "u", "K", None)
    repo.add_measurement(MeasurementRecord(subject, metric, 1.5, "2026-01-01", src, "high", "why", SNAPSHOT, "n", "soon", "r"))
    repo.add_training_set(TrainingSetRecord(repo.get_or_create_exercise("Squat"), "2026-01-01", 225, "lb", 5, None, 1, src, "x"))
    repo.add_muscle_volume(MuscleVolumeRecord(repo.get_or_create_muscle("chest"), "2026-01-05", 10, src))
    repo.add_meal_log_entry(MealLogRecord("2026-01-01", "Lunch", *range(14), src))
    repo.add_food_log_entry(FoodLogRecord(repo.get_or_create_food("Rice"), "2026-01-01", "12:00", 1, "cup", 200, 1, 45, 4, 0, src))
    repo.commit()
    assert repo.counts() == TableCounts(1, 1, 1, 1, 1)
    assert tuple(knowledge_db.execute("SELECT subject_id, metric_id, value, date_added, notes, recheck_by, recheck_rationale "
                                      "FROM measurements").fetchone()) == (subject, metric, 1.5, SNAPSHOT, "n", "soon", "r")
    assert knowledge_db.execute("SELECT is_warmup, rir FROM training_sets").fetchone()[:] == (1, None)


def test_links_attach_every_measurement_of_the_metric_to_the_fact(knowledge_db):
    from client.measurements_port import MeasurementRecord
    repo = measurements_store.SqliteMeasurementRepository(knowledge_db)
    src = repo.get_or_create_source("s", "n", "d", None, SNAPSHOT, "user")
    subject = repo.get_or_create_subject("body-composition")
    for key, n in (("weight_lb", 2), ("waist_in", 1)):
        metric = repo.get_or_create_metric(key, "u", key, None)
        for i in range(n):
            repo.add_measurement(MeasurementRecord(subject, metric, 1.0, f"2026-01-0{i + 1}", src, "high", None, SNAPSHOT))
    knowledge_db.execute("INSERT INTO facts (statement, subject_id, source_key, trust_level, freshness) VALUES ('f', ?, 'p-2', 'high', 'unreviewed')", (subject,))
    knowledge_db.commit()
    links = measurements_store.SqliteFactMeasurementLinks(knowledge_db)
    fact_id = links.fact_id("p-2")
    assert fact_id is not None and links.fact_id("nope") is None
    assert links.link_metric(fact_id, "weight_lb") == 2
    assert links.link_metric(fact_id, "no_such_metric") == 0
    links.commit()
    assert knowledge_db.execute("SELECT COUNT(*) FROM fact_measurements WHERE fact_id = ?", (fact_id,)).fetchone()[0] == 2
