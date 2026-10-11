"""SQLite adapters for the measurement ports: the build database's measurement, metric, exercise, food and
muscle tables (`MeasurementRepository`) and the fact <-> measurement link (`FactMeasurementLinks`).

Each adapter is given the build database connection of the step. Lookups are plain lookup-or-insert; the
per-run caches that skip repeated lookups are the service's.
"""
from typing import Optional

from client.measurements_port import (FoodLogRecord, MealLogRecord, MeasurementRecord, MuscleVolumeRecord,
                                      TableCounts, TrainingSetRecord)


class SqliteMeasurementRepository:
    def __init__(self, con):
        self._cur = con.cursor()
        self._con = con

    def source_id(self, citekey: str) -> Optional[int]:
        row = self._cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
        return row[0] if row else None

    def get_or_create_source(self, citekey, name, description, origin_path, retrieved_date, author) -> int:
        cur = self._cur
        row = cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
        if row:
            return row[0]
        cur.execute("""INSERT INTO sources (citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, origin_path)
            VALUES (?, ?, 'primary', NULL, NULL, NULL, ?, ?, ?)""", (citekey, name, retrieved_date, description, origin_path))
        source_id = cur.lastrowid
        author_id = cur.execute("SELECT id FROM authors WHERE name = ?", (author,)).fetchone()
        if not author_id:
            cur.execute("INSERT INTO authors (name) VALUES (?)", (author,))
            author_id = (cur.lastrowid,)
        cur.execute("INSERT OR IGNORE INTO source_authors (source_id, author_id) VALUES (?, ?)", (source_id, author_id[0]))
        return source_id

    def _get_or_create(self, table: str, column: str, value) -> int:
        row = self._cur.execute(f"SELECT id FROM {table} WHERE {column} = ?", (value,)).fetchone()
        if row:
            return row[0]
        self._cur.execute(f"INSERT INTO {table} ({column}) VALUES (?)", (value,))
        return self._cur.lastrowid

    def get_or_create_subject(self, name):
        return self._get_or_create("subjects", "name", name)

    def get_or_create_metric(self, key, unit, label, good_direction):
        row = self._cur.execute("SELECT id FROM metrics WHERE key = ?", (key,)).fetchone()
        if row:
            return row[0]
        self._cur.execute("INSERT INTO metrics (key, label, unit, good_direction) VALUES (?, ?, ?, ?)",
                          (key, label, unit, good_direction))
        return self._cur.lastrowid

    def get_or_create_exercise(self, name):
        return self._get_or_create("exercises", "name", name)

    def get_or_create_food(self, name):
        return self._get_or_create("foods", "name", name)

    def get_or_create_muscle(self, name):
        return self._get_or_create("muscles", "name", name)

    def add_measurement(self, record: MeasurementRecord) -> None:
        self._cur.execute(
            """INSERT INTO measurements (subject_id, metric_id, value, measured_at, source_id, trust_level,
                                          trust_rationale, date_added, notes, recheck_by, recheck_rationale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.subject_id, record.metric_id, record.value, record.measured_at, record.source_id, record.trust_level, record.trust_rationale,
             record.date_added, record.notes, record.recheck_by, record.recheck_rationale))

    def add_training_set(self, record: TrainingSetRecord) -> None:
        self._cur.execute(
            """INSERT INTO training_sets (exercise_id, measured_at, weight, weight_unit, reps, rir, is_warmup, source_id, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.exercise_id, record.measured_at, record.weight, record.weight_unit, record.reps, record.rir, record.is_warmup, record.source_id, record.notes))

    def add_muscle_volume(self, record: MuscleVolumeRecord) -> None:
        self._cur.execute(
            "INSERT INTO muscle_volume_weekly (muscle_id, week_start, sets, source_id) VALUES (?, ?, ?, ?)",
            (record.muscle_id, record.week_start, record.sets, record.source_id))

    def add_meal_log_entry(self, record: MealLogRecord) -> None:
        self._cur.execute(
            """INSERT INTO meal_log_entries (measured_at, meal, calories_kcal, fat_g, saturated_fat_g, carbs_g, fiber_g,
                                              sugar_g, protein_g, sodium_mg, potassium_mg, cholesterol_mg, vitamin_a,
                                              vitamin_c, calcium, iron, source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.measured_at, record.meal, record.calories_kcal, record.fat_g, record.saturated_fat_g, record.carbs_g, record.fiber_g, record.sugar_g,
             record.protein_g, record.sodium_mg, record.potassium_mg, record.cholesterol_mg, record.vitamin_a, record.vitamin_c, record.calcium, record.iron,
             record.source_id))

    def add_food_log_entry(self, record: FoodLogRecord) -> None:
        self._cur.execute(
            """INSERT INTO food_log_entries (food_id, measured_at, time, serving_qty, serving_size,
                                              calories_kcal, fat_g, carbs_g, protein_g, alcohol_g, source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.food_id, record.measured_at, record.time, record.serving_qty, record.serving_size, record.calories_kcal, record.fat_g, record.carbs_g,
             record.protein_g, record.alcohol_g, record.source_id))

    def counts(self) -> TableCounts:
        def n(table):
            return self._cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return TableCounts(measurements=n("measurements"), training_sets=n("training_sets"),
                           food_log_entries=n("food_log_entries"), meal_log_entries=n("meal_log_entries"),
                           muscle_volume_weekly=n("muscle_volume_weekly"))

    def commit(self) -> None:
        self._con.commit()


class SqliteFactMeasurementLinks:
    def __init__(self, con):
        self._cur = con.cursor()
        self._con = con

    def fact_id(self, source_key: str) -> Optional[int]:
        row = self._cur.execute("SELECT id FROM facts WHERE source_key = ?", (source_key,)).fetchone()
        return row[0] if row else None

    def link_metric(self, fact_id: int, metric_key: str) -> int:
        self._cur.execute(
            """INSERT INTO fact_measurements (fact_id, measurement_id)
               SELECT ?, m.id FROM measurements m JOIN metrics mt ON mt.id = m.metric_id WHERE mt.key = ?""",
            (fact_id, metric_key))
        return self._cur.rowcount

    def commit(self) -> None:
        self._con.commit()
