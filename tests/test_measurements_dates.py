"""measurements.py: the dates it stamps (issue #35). Runs on a tiny fixture vault db
built here; nothing reads the real vault or knowledge-private data."""
import importlib.util
import os
import sqlite3
import sys

import pytest
from schema_helper import full_schema

from test_add_fact import Env, REPO

SNAPSHOT = "2026-09-11"

SCHEMA = {
    "bodycomp_dexa": "date, provider, total_mass_lbs, body_fat_pct, fat_mass_lbs, lean_tissue_lbs, bmc_lbs, rmr_kcal, "
                     "vat_mass_lbs, bmd_total, bmd_z_score, arms_lean_lbs, legs_lean_lbs, height_in",
    "bloodwork": "date, panel, analyte, value, unit, ref_low, ref_high, flag, fasting, on_medication, source_doc, notes",
    "body_measurements": "date, neck_in, shoulders_in, chest_in, upper_arm_in, forearm_in, waist_in, hips_in, thigh_in, calf_in, wrist_in, ankle_in",
    "strength_checkpoints": "date, squat_lbs, squat_bw_ratio, squat_as_of, bench_lbs, bench_bw_ratio, bench_as_of, "
                            "deadlift_lbs, deadlift_bw_ratio, deadlift_as_of, note",
    "nutrition_daily": "date, expenditure_kcal, trend_weight_lbs, weight_lbs, calories_kcal, protein_g, fat_g, carbs_g",
    "scale_readings": "date, weight_lbs, body_fat_pct, source",
    "renpho_scale_readings": "date, weight_lbs, body_fat_pct, fat_free_mass_lbs",
    "muscle_volume_weekly": "date, chest_sets, quads_sets",
    "workout_sets": "date, exercise, completed_weight, weight_unit, completed_reps, rir, is_warmup, notes",
    "jefit_exercise_sets": "date, exercise, weight, reps, weight_unit",
    "mfp_measurements": "date, fitbit_body_fat_pct, fitbit_steps, fitbit_sleep_minutes, weight_lbs",
    "mfp_exercise_log": "date, exercise, type, exercise_calories, exercise_minutes",
    "mfp_nutrition_log": "date, meal, calories_kcal, fat_g, saturated_fat_g, carbs_g, fiber_g, sugar_g, protein_g, sodium_mg, "
                         "potassium_mg, cholesterol_mg, vitamin_a, vitamin_c, calcium, iron",
    "nutrition_food_log": "date, time, food_name, serving_size, serving_qty, calories_kcal, fat_g, carbs_g, protein_g, alcohol_g",
    "micronutrients": "date, iron_mg, source_file",
}
ROWS = {
    "bodycomp_dexa": [("2025-11-15", "x", 150.0, 20.0, 30.0, 110.0, 6.0, 1800, 1.0, 1.2, 0.5, 20.0, 40.0, 70.0),
                      ("2026-06-17", "x", 160.0, 18.0, 29.0, 125.0, 6.5, 1900, 0.9, 1.3, 0.6, 22.0, 42.0, 70.0)],
    "body_measurements": [("2026-01-01", 15, 45, 40, 14, 11, 32, 38, 22, 15, 6.5, 8)],
    "nutrition_daily": [("2026-02-01", 2500, 160.0, 161.0, 2400, 150, 70, 250)],
    "scale_readings": [("2026-02-02", 160.5, 18.5, "s")],
    "workout_sets": [("2026-02-03", "Squat", 225, "lb", 5, 2, 0, None)],
    "mfp_measurements": [("2026-02-04", 18.0, 9000, 420, 160.0)],
    "micronutrients": [("2026-02-05", 12.5, "f.csv")],
}


def make_vault(path):
    con = sqlite3.connect(path)
    for table, cols in SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
        for row in ROWS.get(table, []):
            con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(row))})", row)
    con.commit()
    con.close()


@pytest.fixture
def m03(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", e.private)
    for m in ("paths", "local_paths", "shared", "add_fact", "add_fact_store", "new_fact", "snapshot_date", "revisions", "revisions_store", "backfill_source_keys", "backfill_dates"):
        sys.modules.pop(m, None)
    monkeypatch.syspath_prepend(REPO)
    spec = importlib.util.spec_from_file_location("ing_03", os.path.join(REPO, "ingest", "measurements.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    vault = str(tmp_path / "bodybuilding.db")
    make_vault(vault)
    mod.VAULT_DB = vault

    class Ctx:
        env = e
        module = mod

        @staticmethod
        def build():
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON;")
            con.executescript(full_schema())
            for ck in ("bodyspec-dexa-2026-06-17", "bodyspec-dexa-2025-11-15", "labcorp-2025-01-24", "manual-tape-measurements",
                       "strength-checkpoints-script", "unlocated-crp-esr-notes"):
                con.execute("INSERT INTO sources (citekey, name, source_type) VALUES (?, ?, 'primary')", (ck, ck))
            mod.DATA_DIR = e.data_dir
            mod.run(con)
            return con

        @staticmethod
        def write_snapshot(text):
            with open(os.path.join(e.data_dir, "measurements_snapshot.json"), "w") as f:
                f.write(text)

    return Ctx


# ------------------------------------------------ the snapshot date comes from the data (#35)

def test_rows_and_vault_sources_carry_the_snapshot_date_from_the_data(m03):
    m03.write_snapshot('{"synced_at": "2026-09-11"}')
    con = m03.build()
    assert con.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] > 20
    assert {r[0] for r in con.execute("SELECT date_added FROM measurements")} == {SNAPSHOT}
    assert {r["retrieved_date"] for r in con.execute("SELECT retrieved_date FROM sources WHERE citekey LIKE 'vault-db-%'")} == {SNAPSHOT}
    assert con.execute("SELECT COUNT(*) FROM sources WHERE description LIKE '%as of 2026-09-11%'").fetchone()[0] > 0


def test_the_date_is_whatever_the_data_says_not_a_constant(m03):
    m03.write_snapshot('{"synced_at": "2027-02-03T04:05:06+00:00"}')
    con = m03.build()
    assert {r[0] for r in con.execute("SELECT date_added FROM measurements")} == {"2027-02-03T04:05:06+00:00"}
    assert con.execute("SELECT COUNT(*) FROM sources WHERE description LIKE '%as of 2027-02-03T04:05:06+00:00%'").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM sources WHERE description LIKE '%2026-09-11%'").fetchone()[0] == 0


def test_measured_values_do_not_depend_on_the_snapshot_date(m03):
    def rows(date):
        m03.write_snapshot('{"synced_at": "%s"}' % date)
        con = m03.build()
        return [tuple(r) for r in con.execute(
            "SELECT subject_id, metric_id, value, measured_at, source_id, trust_level, notes FROM measurements ORDER BY id")]
    assert rows("2026-09-11") == rows("2030-01-01")


@pytest.mark.parametrize("content, msg", [
    (None, "is missing"), ("{nope", "not valid JSON"), ("[]", "synced_at"), ("{}", "synced_at"),
    ('{"synced_at": null}', "synced_at"), ('{"synced_at": "soon"}', "synced_at"), ('{"synced_at": 20260911}', "synced_at")])
def test_a_missing_or_bad_snapshot_file_fails_the_build_and_names_the_file(m03, content, msg):
    if content is not None:
        m03.write_snapshot(content)
    with pytest.raises(RuntimeError, match=msg) as e:
        m03.build()
    assert "measurements_snapshot.json" in str(e.value)
    if content is None:
        assert "ingest.backfill_dates" in str(e.value)


def test_backfill_dates_output_is_what_03_reads(m03, tmp_path):
    from ingest import backfill_dates
    backfill_dates.backfill_dates(m03.env.data_dir, apply=True)
    con = m03.build()
    assert {r[0] for r in con.execute("SELECT date_added FROM measurements")} == {SNAPSHOT}
