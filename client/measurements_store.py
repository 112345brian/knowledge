"""SQLite adapters for the measurement aggregate of `build_repositories_port`: `MeasurementRepository` (the
measurement, metric, exercise, food and muscle tables) and the fact <-> measurement link of `FactRepository`.

Each adapter is given the build database connection of the step. They store what they are told and decide
nothing: which source a reading belongs to, which subject, which label a metric gets and whether a name is blank
are `measurements_service`'s and `measurement_rules`'. Each `add` allocates its row when called, in call order
(the golden dump compares the ids). Nothing here commits; the step's unit of work does.
"""
import sqlite3
from typing import Optional

import build_records as r

MEAL_LOG_COLUMNS = ("calories_kcal", "fat_g", "saturated_fat_g", "carbs_g", "fiber_g", "sugar_g", "protein_g",
                    "sodium_mg", "potassium_mg", "cholesterol_mg", "vitamin_a", "vitamin_c", "calcium", "iron")
_INSERT_MEAL = ("INSERT INTO meal_log_entries (measured_at, meal, " + ", ".join(MEAL_LOG_COLUMNS) + ", source_id) "
                "VALUES (" + ", ".join("?" * (len(MEAL_LOG_COLUMNS) + 3)) + ")")


class SqliteMeasurementRepository:
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def _get_or_create(self, table: str, name: str) -> int:
        row = self._con.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
        if row:
            return row[0]
        return self._con.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid

    def find_metric(self, key: str) -> Optional[r.MetricId]:
        row = self._con.execute("SELECT id FROM metrics WHERE key = ?", (key,)).fetchone()
        return r.MetricId(row[0]) if row else None

    def add_metric(self, metric: r.NewMetric) -> r.MetricId:
        cur = self._con.execute("INSERT INTO metrics (key, label, unit, good_direction) VALUES (?, ?, ?, ?)",
                                (metric.key, metric.label, metric.unit, metric.good_direction))
        return r.MetricId(cur.lastrowid)

    def add(self, measurement: r.NewMeasurement) -> None:
        m = measurement
        self._con.execute(
            """INSERT INTO measurements (subject_id, metric_id, value, measured_at, source_id, trust_level,
                                          trust_rationale, date_added, notes, recheck_by, recheck_rationale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m.subject_id, m.metric_id, m.value, m.measured_at, m.source_id, m.trust_level, m.trust_rationale,
             m.date_added, m.notes, m.recheck_by, m.recheck_rationale))

    def exercise(self, name: str) -> int:
        return self._get_or_create("exercises", name)

    def food(self, name: str) -> int:
        return self._get_or_create("foods", name)

    def muscle(self, name: str) -> int:
        return self._get_or_create("muscles", name)

    def add_training_set(self, training_set: r.NewTrainingSet) -> None:
        t = training_set
        self._con.execute(
            """INSERT INTO training_sets (exercise_id, measured_at, weight, weight_unit, reps, rir, is_warmup, source_id, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t.exercise_id, t.measured_at, t.weight, t.weight_unit, t.reps, t.rir, t.is_warmup, t.source_id, t.notes))

    def add_food_log_entry(self, entry: r.NewFoodLogEntry) -> None:
        e = entry
        self._con.execute(
            """INSERT INTO food_log_entries (food_id, measured_at, time, serving_qty, serving_size,
                                              calories_kcal, fat_g, carbs_g, protein_g, alcohol_g, source_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (e.food_id, e.measured_at, e.time, e.serving_qty, e.serving_size, e.calories_kcal, e.fat_g, e.carbs_g,
             e.protein_g, e.alcohol_g, e.source_id))

    def add_meal_log_entry(self, entry: r.NewMealLogEntry) -> None:
        self._con.execute(_INSERT_MEAL, (entry.measured_at, entry.meal,
                                         *(entry.nutrients[c] for c in MEAL_LOG_COLUMNS), entry.source_id))

    def add_muscle_volume(self, volume: r.NewMuscleVolume) -> None:
        self._con.execute("INSERT INTO muscle_volume_weekly (muscle_id, week_start, sets, source_id) VALUES (?, ?, ?, ?)",
                          (volume.muscle_id, volume.week_start, volume.sets, volume.source_id))

    def totals(self) -> r.MeasurementTotals:
        def n(table: str) -> int:
            return self._con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return r.MeasurementTotals(measurements=n("measurements"), training_sets=n("training_sets"),
                                   food_log_entries=n("food_log_entries"), meal_log_entries=n("meal_log_entries"),
                                   muscle_volume_weekly=n("muscle_volume_weekly"))


class SqliteFactMeasurementLinks:
    """The two members of `FactRepository` the link step calls (the facts adapter is the facts aggregate's; this
    one answers only what the step needs: which fact a source key is, and the measurement links)."""

    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def find_by_source_key(self, source_key: str) -> Optional[r.FactId]:
        row = self._con.execute("SELECT id FROM facts WHERE source_key = ?", (source_key,)).fetchone()
        return r.FactId(row[0]) if row else None

    def link_measurements(self, fact_id: r.FactId, metric_key: str) -> int:
        return self._con.execute(
            """INSERT INTO fact_measurements (fact_id, measurement_id)
               SELECT ?, m.id FROM measurements m JOIN metrics mt ON mt.id = m.metric_id WHERE mt.key = ?""",
            (fact_id, metric_key)).rowcount
