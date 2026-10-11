"""Measurements use cases: load a vault's numeric readings into the knowledge database, and link facts to the
measurements they cite. Reaches the vault and the database only through ports (`vault_reader_port`,
`measurements_port`); the transformations are the domain's (`measurement_rules`).

One load's lookup caches (subjects, metrics, exercises, foods, muscles) live in a `_Run` made for that load and
dropped with it: nothing survives between runs and nothing is module state.
"""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence

from client import measurement_rules
from client.measurements_port import (FactMeasurementLinks, FoodLogRecord, MealLogRecord, MeasurementRecord,
                                      MeasurementRepository, MuscleVolumeRecord, TrainingSetRecord)
from client.vault_reader_port import VaultReader

SOURCE_AUTHOR = "user"      # the author recorded on the sources a load creates from vault tables


@dataclass(frozen=True)
class MeasurementsDeps:
    vault: VaultReader
    repository: MeasurementRepository


@dataclass(frozen=True)
class FactLinksDeps:
    links: FactMeasurementLinks


class _Run:
    """One load's id caches and snapshot date."""

    def __init__(self, repository: MeasurementRepository, snapshot_date: str):
        self.repository = repository
        self.snapshot_date = snapshot_date
        self.subjects: Dict[str, int] = {}
        self.metrics: Dict[str, int] = {}
        self.exercises: Dict[str, int] = {}
        self.foods: Dict[str, int] = {}
        self.muscles: Dict[str, int] = {}

    def subject_id(self, name):
        if name not in self.subjects:
            self.subjects[name] = self.repository.get_or_create_subject(name)
        return self.subjects[name]

    def metric_id(self, key, unit):
        if key not in self.metrics:
            label, good_direction = measurement_rules.metric_label(key)
            self.metrics[key] = self.repository.get_or_create_metric(key, unit, label, good_direction)
        return self.metrics[key]

    def exercise_id(self, name):
        if name not in self.exercises:
            self.exercises[name] = self.repository.get_or_create_exercise(name)
        return self.exercises[name]

    def food_id(self, name):
        if name not in self.foods:
            self.foods[name] = self.repository.get_or_create_food(name)
        return self.foods[name]

    def muscle_id(self, name):
        if name not in self.muscles:
            self.muscles[name] = self.repository.get_or_create_muscle(name)
        return self.muscles[name]

    def add_measurements(self, measurements: Iterable[measurement_rules.Measurement], source_ids: Mapping[str, int]):
        for m in measurements:
            subject_id = self.subject_id(m.subject)
            metric_id = self.metric_id(m.metric, m.unit)
            self.repository.add_measurement(MeasurementRecord(
                subject_id=subject_id, metric_id=metric_id, value=m.value, measured_at=m.measured_at,
                source_id=source_ids[m.source], trust_level=m.trust_level, trust_rationale=m.trust_rationale,
                date_added=self.snapshot_date, notes=m.notes, recheck_by=m.recheck_by,
                recheck_rationale=m.recheck_rationale))

    def create_sources(self, group: Mapping[str, Sequence[Any]]) -> Dict[str, int]:
        """get_or_create_source for each (citekey, name, description, origin) of a vault_db_sources group."""
        return {key: self.repository.get_or_create_source(citekey, name, description, origin_path,
                                                          self.snapshot_date, SOURCE_AUTHOR)
                for key, (citekey, name, description, origin_path) in group.items()}


def load_measurements(deps: MeasurementsDeps, *, snapshot_date: str, notes_vault: str,
                      manual_sources: Mapping[str, str], scans: Sequence[Sequence[str]]
                      ) -> measurement_rules.LoadSummary:
    """Insert the vault's readings, in the vault tables' own order, and return what was written. `manual_sources`
    maps a source key to the citekey step 01 seeded; `scans` is [(scan date, source key)] of the DEXA scans."""
    vault, repo = deps.vault, deps.repository
    run = _Run(repo, snapshot_date)
    inserted_before = repo.counts().measurements

    ids: Dict[str, int] = {}
    for name, citekey in manual_sources.items():
        source_id = repo.source_id(citekey)
        if source_id is None:
            raise RuntimeError(f"source citekey not found: {citekey} (run seed_sources.py first)")
        ids[name] = source_id

    # ---------------- DEXA: both scans, full field set from bodycomp_dexa ----------------
    for scan_date, source in scans:
        scan = vault.dexa_scan(scan_date)
        if scan is None:
            raise RuntimeError(f"no bodycomp_dexa row dated {scan_date} in the vault (measurements_config.json scans)")
        run.add_measurements(measurement_rules.dexa_measurements(scan, scan_date, source), ids)

    # ---------------- Bloodwork ----------------
    for row in vault.bloodwork():
        m = measurement_rules.bloodwork_measurement(row)
        if m is not None:
            run.add_measurements([m], ids)

    # ---------------- Manual tape measurements ----------------
    for row in vault.body_measurements():
        run.add_measurements(measurement_rules.tape_measurements(row), ids)

    # ---------------- Strength checkpoints (latest snapshot only) ----------------
    latest = vault.latest_strength_checkpoint()
    if latest:
        run.add_measurements(measurement_rules.strength_measurements(latest), ids)

    # ---------------- Daily/weekly summary tables ----------------
    summary, raw = measurement_rules.vault_db_sources(notes_vault, measurement_rules.stale_note(snapshot_date))
    ids.update(run.create_sources(summary))

    for row in vault.nutrition_daily():
        run.add_measurements(measurement_rules.nutrition_measurements(row), ids)
    for row in vault.scale_readings():
        run.add_measurements(measurement_rules.scale_measurements(row), ids)
    for row in vault.renpho_scale_readings():
        run.add_measurements(measurement_rules.renpho_measurements(row), ids)

    volume = vault.muscle_volume_weekly()
    for row in volume.rows:
        for col in volume.columns:
            if row[col] is None:
                continue
            muscle_id = run.muscle_id(measurement_rules.muscle_name(col))
            repo.add_muscle_volume(MuscleVolumeRecord(muscle_id=muscle_id, week_start=row["date"], sets=row[col],
                                                      source_id=ids["volume"]))

    # ---------------- Raw per-set / per-food / Fitbit / micronutrient logs ----------------
    ids.update(run.create_sources(raw))

    for s in vault.workout_sets():
        repo.add_training_set(TrainingSetRecord(
            exercise_id=run.exercise_id(s.exercise or "Unknown exercise"), measured_at=s.date, weight=s.completed_weight,
            weight_unit=s.weight_unit or "lb", reps=s.completed_reps, rir=s.rir, is_warmup=1 if s.is_warmup else 0,
            source_id=ids["sets"], notes=s.notes))

    for s in vault.jefit_exercise_sets():
        repo.add_training_set(TrainingSetRecord(
            exercise_id=run.exercise_id(s.exercise or "Unknown exercise"), measured_at=s.date, weight=s.weight,
            weight_unit=s.weight_unit or "lb", reps=s.reps, rir=None, is_warmup=0, source_id=ids["jefit"], notes=None))

    for row in vault.mfp_measurements():
        run.add_measurements(measurement_rules.mfp_measurements(row), ids)

    for row in vault.mfp_exercise_log():
        run.add_measurements(measurement_rules.exercise_log_measurements(row), ids)

    for m in vault.mfp_nutrition_log():
        repo.add_meal_log_entry(MealLogRecord(
            measured_at=m.date, meal=m.meal, calories_kcal=m.calories_kcal, fat_g=m.fat_g,
            saturated_fat_g=m.saturated_fat_g, carbs_g=m.carbs_g, fiber_g=m.fiber_g, sugar_g=m.sugar_g,
            protein_g=m.protein_g, sodium_mg=m.sodium_mg, potassium_mg=m.potassium_mg,
            cholesterol_mg=m.cholesterol_mg, vitamin_a=m.vitamin_a, vitamin_c=m.vitamin_c, calcium=m.calcium,
            iron=m.iron, source_id=ids["mfp_n"]))

    for f in vault.nutrition_food_log():
        repo.add_food_log_entry(FoodLogRecord(
            food_id=run.food_id(f.food_name or "Unknown food"), measured_at=f.date, time=f.time,
            serving_qty=f.serving_qty, serving_size=f.serving_size, calories_kcal=f.calories_kcal, fat_g=f.fat_g,
            carbs_g=f.carbs_g, protein_g=f.protein_g, alcohol_g=f.alcohol_g, source_id=ids["food"]))

    micro = vault.micronutrients()
    for row in micro.rows:
        run.add_measurements(measurement_rules.micronutrient_measurements(row, micro.columns), ids)

    repo.commit()
    counts = repo.counts()
    return measurement_rules.LoadSummary(
        inserted=counts.measurements - inserted_before, subjects=len(run.subjects), metrics=len(run.metrics),
        training_sets=counts.training_sets, exercises=len(run.exercises), food_log_entries=counts.food_log_entries,
        foods=len(run.foods), meal_log_entries=counts.meal_log_entries,
        muscle_volume_rows=counts.muscle_volume_weekly, muscles=len(run.muscles))


def link_fact_measurements(deps: FactLinksDeps, items: Iterable[Mapping[str, Any]]) -> int:
    """One `fact_measurements` row per measurement of the metric each loaded fact cites in
    `measurement_metric_link`; returns the number of links made. A fact the facts step skipped is not linked."""
    links = deps.links
    linked = 0
    for item in items:
        metric = item.get("measurement_metric_link")
        if not metric:
            continue
        fact_id = links.fact_id(item["_source_key"])
        if fact_id is None:
            continue   # the facts step skipped this entry (an invalid one), so there is nothing to link
        linked += links.link_metric(fact_id, metric)
    links.commit()
    return linked
