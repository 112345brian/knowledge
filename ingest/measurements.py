"""Ingest every structured, repeatable numeric reading: DEXA (both scans,
full field set), bloodwork, manual tape measurements, computed strength
checkpoints, daily/weekly summary tables, and Fitbit/micronutrient logs go
into `measurements` (subject_id/metric_id resolved via get-or-create
helpers, never written as repeated text). Per-set training, per-food, and
per-meal logs go into their own event tables instead (`training_sets`,
`food_log_entries`, `meal_log_entries`) with `exercises`/`foods` as proper
entity tables -- a set or a food-log line is one event with several
co-occurring attributes, not independent measurements sharing a date.
Run after seed_sources.py (needs its citekeys to exist).

Source: the bodybuilding vault's own bodybuilding.db (see paths.py -> BODYBUILDING_VAULT)
"""
import sqlite3, os

from ingest import measurement_rules
from paths import BODYBUILDING_VAULT as VAULT, PRIVATE_DATA_DIR as DATA_DIR
from ingest.snapshot_date import read_snapshot_date

VAULT_DB = os.path.expanduser(f"{VAULT}/bodybuilding.db")
# The vault db has no per-row load timestamp (only the date each reading was taken), so the
# date_added of these rows, and the retrieved_date of the vault-db sources, is the date the
# snapshot was taken. It is read from the data (measurements_snapshot.json, written once by
# backfill_dates.py) in run(); a missing or malformed file fails the build. #35.
SNAPSHOT_DATE = None

_subject_cache = {}
_metric_cache = {}


def get_or_create_subject(cur, name):
    if name in _subject_cache:
        return _subject_cache[name]
    row = cur.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
    if not row:
        cur.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
        sid = cur.lastrowid
    else:
        sid = row[0]
    _subject_cache[name] = sid
    return sid


def get_or_create_metric(cur, key, unit):
    if key in _metric_cache:
        return _metric_cache[key]
    row = cur.execute("SELECT id FROM metrics WHERE key = ?", (key,)).fetchone()
    if not row:
        label, good_direction = measurement_rules.metric_label(key)
        cur.execute("INSERT INTO metrics (key, label, unit, good_direction) VALUES (?, ?, ?, ?)",
                    (key, label, unit, good_direction))
        mid = cur.lastrowid
    else:
        mid = row[0]
    _metric_cache[key] = mid
    return mid


def insert_measurement(cur, *, subject, metric, value, unit, measured_at, source_id, trust_level,
                        trust_rationale, notes=None, recheck_by=None, recheck_rationale=None):
    subject_id = get_or_create_subject(cur, subject)
    metric_id = get_or_create_metric(cur, metric, unit)
    cur.execute(
        """INSERT INTO measurements (subject_id, metric_id, value, measured_at, source_id, trust_level,
                                      trust_rationale, date_added, notes, recheck_by, recheck_rationale)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (subject_id, metric_id, value, measured_at, source_id, trust_level, trust_rationale,
         SNAPSHOT_DATE, notes, recheck_by, recheck_rationale),
    )


def get_source_id(cur, citekey):
    row = cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
    if not row:
        raise RuntimeError(f"source citekey not found: {citekey} (run seed_sources.py first)")
    return row[0]


def get_or_create_source(cur, citekey, name, description, origin_path=None):
    row = cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
    if row:
        return row[0]
    cur.execute("""INSERT INTO sources (citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, origin_path)
        VALUES (?, ?, 'primary', NULL, NULL, NULL, ?, ?, ?)""", (citekey, name, SNAPSHOT_DATE, description, origin_path))
    source_id = cur.lastrowid
    author_id = cur.execute("SELECT id FROM authors WHERE name = 'user'").fetchone()
    if not author_id:
        cur.execute("INSERT INTO authors (name) VALUES ('user')")
        author_id = (cur.lastrowid,)
    cur.execute("INSERT OR IGNORE INTO source_authors (source_id, author_id) VALUES (?, ?)", (source_id, author_id[0]))
    return source_id


_exercise_cache = {}
_food_cache = {}


def get_or_create_exercise(cur, name):
    if name in _exercise_cache:
        return _exercise_cache[name]
    row = cur.execute("SELECT id FROM exercises WHERE name = ?", (name,)).fetchone()
    eid = row[0] if row else None
    if eid is None:
        cur.execute("INSERT INTO exercises (name) VALUES (?)", (name,))
        eid = cur.lastrowid
    _exercise_cache[name] = eid
    return eid


def get_or_create_food(cur, name):
    if name in _food_cache:
        return _food_cache[name]
    row = cur.execute("SELECT id FROM foods WHERE name = ?", (name,)).fetchone()
    fid = row[0] if row else None
    if fid is None:
        cur.execute("INSERT INTO foods (name) VALUES (?)", (name,))
        fid = cur.lastrowid
    _food_cache[name] = fid
    return fid


_muscle_cache = {}


def get_or_create_muscle(cur, name):
    if name in _muscle_cache:
        return _muscle_cache[name]
    row = cur.execute("SELECT id FROM muscles WHERE name = ?", (name,)).fetchone()
    mid = row[0] if row else None
    if mid is None:
        cur.execute("INSERT INTO muscles (name) VALUES (?)", (name,))
        mid = cur.lastrowid
    _muscle_cache[name] = mid
    return mid


def insert_measurements(cur, measurements, source_ids):
    for m in measurements:
        insert_measurement(cur, subject=m.subject, metric=m.metric, value=m.value, unit=m.unit, measured_at=m.measured_at,
                           source_id=source_ids[m.source], trust_level=m.trust_level, trust_rationale=m.trust_rationale,
                           notes=m.notes, recheck_by=m.recheck_by, recheck_rationale=m.recheck_rationale)


def create_sources(cur, group):
    """get_or_create_source for each (citekey, name, description, origin) of a vault_db_sources group."""
    return {key: get_or_create_source(cur, *spec) for key, spec in group.items()}


def run(con):
    global SNAPSHOT_DATE
    SNAPSHOT_DATE = read_snapshot_date(DATA_DIR)  # raises, naming the file, if it is not in the data
    vault = sqlite3.connect(VAULT_DB)
    vault.row_factory = sqlite3.Row
    cur = con.cursor()
    _subject_cache.clear()
    _metric_cache.clear()
    _exercise_cache.clear()
    _food_cache.clear()
    _muscle_cache.clear()
    inserted_before = cur.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]

    ids = {name: get_source_id(cur, citekey) for name, citekey in measurement_rules.MANUAL_SOURCES.items()}

    # ---------------- DEXA: both scans, full field set from bodycomp_dexa ----------------
    for scan_date, source in measurement_rules.DEXA_SCANS:
        scan = vault.execute("SELECT * FROM bodycomp_dexa WHERE date = ?", (scan_date,)).fetchone()
        insert_measurements(cur, measurement_rules.dexa_measurements(scan, scan_date, source), ids)

    # ---------------- Bloodwork ----------------
    for row in vault.execute("SELECT * FROM bloodwork"):
        m = measurement_rules.bloodwork_measurement(row)
        if m is not None:
            insert_measurements(cur, [m], ids)

    # ---------------- Manual tape measurements ----------------
    for row in vault.execute("SELECT * FROM body_measurements"):
        insert_measurements(cur, measurement_rules.tape_measurements(row), ids)

    # ---------------- Strength checkpoints (latest snapshot only) ----------------
    latest = vault.execute("SELECT * FROM strength_checkpoints ORDER BY date DESC LIMIT 1").fetchone()
    if latest:
        insert_measurements(cur, measurement_rules.strength_measurements(latest), ids)

    # ---------------- Daily/weekly summary tables ----------------
    summary, raw = measurement_rules.vault_db_sources(VAULT, measurement_rules.stale_note(SNAPSHOT_DATE))
    ids.update(create_sources(cur, summary))

    for row in vault.execute("SELECT * FROM nutrition_daily"):
        insert_measurements(cur, measurement_rules.nutrition_measurements(row), ids)
    for row in vault.execute("SELECT * FROM scale_readings"):
        insert_measurements(cur, measurement_rules.scale_measurements(row), ids)
    for row in vault.execute("SELECT * FROM renpho_scale_readings"):
        insert_measurements(cur, measurement_rules.renpho_measurements(row), ids)

    cols = [d[0] for d in vault.execute("SELECT * FROM muscle_volume_weekly LIMIT 1").description if d[0] != "date"]
    for row in vault.execute("SELECT * FROM muscle_volume_weekly"):
        for col in cols:
            if row[col] is None:
                continue
            muscle_id = get_or_create_muscle(cur, measurement_rules.muscle_name(col))
            cur.execute(
                "INSERT INTO muscle_volume_weekly (muscle_id, week_start, sets, source_id) VALUES (?, ?, ?, ?)",
                (muscle_id, row["date"], row[col], ids["volume"]),
            )

    # ---------------- Raw per-set / per-food / Fitbit / micronutrient logs ----------------
    ids.update(create_sources(cur, raw))

    for row in vault.execute("SELECT * FROM workout_sets"):
        exercise_id = get_or_create_exercise(cur, row["exercise"] or "Unknown exercise")
        cur.execute(
            """INSERT INTO training_sets (exercise_id, measured_at, weight, weight_unit, reps, rir, is_warmup, source_id, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exercise_id, row["date"], row["completed_weight"], row["weight_unit"] or "lb", row["completed_reps"],
             row["rir"], 1 if row["is_warmup"] else 0, ids["sets"], row["notes"]),
        )

    for row in vault.execute("SELECT * FROM jefit_exercise_sets"):
        exercise_id = get_or_create_exercise(cur, row["exercise"] or "Unknown exercise")
        cur.execute(
            """INSERT INTO training_sets (exercise_id, measured_at, weight, weight_unit, reps, rir, is_warmup, source_id)
               VALUES (?, ?, ?, ?, ?, NULL, 0, ?)""",
            (exercise_id, row["date"], row["weight"], row["weight_unit"] or "lb", row["reps"], ids["jefit"]),
        )

    for row in vault.execute("SELECT * FROM mfp_measurements"):
        insert_measurements(cur, measurement_rules.mfp_measurements(row), ids)

    for row in vault.execute("SELECT * FROM mfp_exercise_log"):
        insert_measurements(cur, measurement_rules.exercise_log_measurements(row), ids)

    for row in vault.execute("SELECT * FROM mfp_nutrition_log"):
        cur.execute(
            """INSERT INTO meal_log_entries (measured_at, meal, calories_kcal, fat_g, saturated_fat_g, carbs_g, fiber_g,
                                              sugar_g, protein_g, sodium_mg, potassium_mg, cholesterol_mg, vitamin_a,
                                              vitamin_c, calcium, iron, source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["date"], row["meal"], row["calories_kcal"], row["fat_g"], row["saturated_fat_g"], row["carbs_g"],
             row["fiber_g"], row["sugar_g"], row["protein_g"], row["sodium_mg"], row["potassium_mg"],
             row["cholesterol_mg"], row["vitamin_a"], row["vitamin_c"], row["calcium"], row["iron"], ids["mfp_n"]),
        )

    for row in vault.execute("SELECT * FROM nutrition_food_log"):
        food_id = get_or_create_food(cur, row["food_name"] or "Unknown food")
        cur.execute(
            """INSERT INTO food_log_entries (food_id, measured_at, time, serving_qty, serving_size,
                                              calories_kcal, fat_g, carbs_g, protein_g, alcohol_g, source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (food_id, row["date"], row["time"], row["serving_qty"], row["serving_size"],
             row["calories_kcal"], row["fat_g"], row["carbs_g"], row["protein_g"], row["alcohol_g"], ids["food"]),
        )

    micro_cols = [d[0] for d in vault.execute("SELECT * FROM micronutrients LIMIT 1").description if d[0] not in ("date", "source_file")]
    for row in vault.execute("SELECT * FROM micronutrients"):
        insert_measurements(cur, measurement_rules.micronutrient_measurements(row, micro_cols), ids)

    con.commit()
    vault.close()
    inserted = cur.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] - inserted_before
    n_sets = cur.execute("SELECT COUNT(*) FROM training_sets").fetchone()[0]
    n_foods = cur.execute("SELECT COUNT(*) FROM food_log_entries").fetchone()[0]
    n_meals = cur.execute("SELECT COUNT(*) FROM meal_log_entries").fetchone()[0]
    n_volume = cur.execute("SELECT COUNT(*) FROM muscle_volume_weekly").fetchone()[0]
    print(f"[measurements] inserted {inserted} measurement rows "
          f"({len(_subject_cache)} subjects, {len(_metric_cache)} metrics touched)")
    print(f"  plus {n_sets} training_sets ({len(_exercise_cache)} exercises), "
          f"{n_foods} food_log_entries ({len(_food_cache)} foods), {n_meals} meal_log_entries, "
          f"{n_volume} muscle_volume_weekly rows ({len(_muscle_cache)} muscles)")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
