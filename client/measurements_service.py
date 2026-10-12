"""Measurements use cases: load a vault's numeric readings into the knowledge database, and link facts to the
measurements they cite. Reaches the vault through `vault_reader_port` and the database through the build's
repositories (`build_repositories_port`: measurements, sources, subjects, facts, unit of work); the
transformations are the domain's (`measurement_rules`). The adapters decide nothing: a source is looked up and
then created here, a subject is found and then added, a metric is found and then given its curated label.

One load's lookup caches (subjects, metrics, exercises, foods, muscles) live in a `_Run` made for that load and
dropped with it: nothing survives between runs and nothing is module state.
"""
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Sequence

import build_records as r
from build_repositories_port import MeasurementRepository, SourceRepository, UnitOfWork
from client import measurement_rules
from client.vault_reader_port import VaultReader

SOURCE_AUTHOR = "user"      # the author recorded on the sources a load creates from vault tables
SOURCE_TYPE = "primary"     # and their source_type

# the nutrient columns of a meal log row, by the name the vault and the table share
MEAL_LOG_NUTRIENTS = ("calories_kcal", "fat_g", "saturated_fat_g", "carbs_g", "fiber_g", "sugar_g", "protein_g",
                      "sodium_mg", "potassium_mg", "cholesterol_mg", "vitamin_a", "vitamin_c", "calcium", "iron")


class SubjectDirectory(Protocol):
    """The two members of `build_repositories_port.SubjectRepository` the load calls."""
    def find_by_name(self, name: str) -> Optional[r.SubjectId]: ...
    def add(self, name: str, domain: Optional[str] = None) -> r.SubjectId: ...


class FactLinks(Protocol):
    """The two members of `build_repositories_port.FactRepository` the link step calls."""
    def find_by_source_key(self, source_key: str) -> Optional[r.FactId]: ...
    def link_measurements(self, fact_id: r.FactId, metric_key: str) -> int: ...


@dataclass(frozen=True)
class MeasurementsDeps:
    vault: VaultReader
    measurements: MeasurementRepository
    sources: SourceRepository
    subjects: SubjectDirectory
    unit_of_work: UnitOfWork


@dataclass(frozen=True)
class FactLinksDeps:
    facts: FactLinks
    unit_of_work: UnitOfWork


class _Run:
    """One load's id caches and snapshot date."""

    def __init__(self, deps: MeasurementsDeps, snapshot_date: str):
        self.repository = deps.measurements
        self.sources = deps.sources
        self.subject_directory = deps.subjects
        self.snapshot_date = snapshot_date
        self.subjects: Dict[str, r.SubjectId] = {}
        self.metrics: Dict[str, r.MetricId] = {}
        self.exercises: Dict[str, int] = {}
        self.foods: Dict[str, int] = {}
        self.muscles: Dict[str, int] = {}

    def subject_id(self, name: str) -> r.SubjectId:
        if name not in self.subjects:
            found = self.subject_directory.find_by_name(name)
            self.subjects[name] = found if found is not None else self.subject_directory.add(name)
        return self.subjects[name]

    def metric_id(self, key: str, unit: str) -> r.MetricId:
        if key not in self.metrics:
            found = self.repository.find_metric(key)
            if found is None:
                label, good_direction = measurement_rules.metric_label(key)
                found = self.repository.add_metric(
                    r.NewMetric(key=key, label=label, unit=unit, good_direction=good_direction))
            self.metrics[key] = found
        return self.metrics[key]

    def exercise_id(self, name: str) -> int:
        if name not in self.exercises:
            self.exercises[name] = self.repository.exercise(name)
        return self.exercises[name]

    def food_id(self, name: str) -> int:
        if name not in self.foods:
            self.foods[name] = self.repository.food(name)
        return self.foods[name]

    def muscle_id(self, name: str) -> int:
        if name not in self.muscles:
            self.muscles[name] = self.repository.muscle(name)
        return self.muscles[name]

    def add_measurements(self, measurements: Iterable[measurement_rules.Measurement],
                         source_ids: Mapping[str, r.SourceId]) -> None:
        for m in measurements:
            subject_id = self.subject_id(m.subject)
            metric_id = self.metric_id(m.metric, m.unit)
            self.repository.add(r.NewMeasurement(
                subject_id=subject_id, metric_id=metric_id, value=m.value, measured_at=m.measured_at,
                source_id=source_ids[m.source], trust_level=m.trust_level, trust_rationale=m.trust_rationale,
                date_added=self.snapshot_date, notes=m.notes, recheck_by=m.recheck_by,
                recheck_rationale=m.recheck_rationale))

    def create_sources(self, group: Mapping[str, Sequence[Any]]) -> Dict[str, r.SourceId]:
        """The source of each (citekey, name, description, origin) of a vault_db_sources group, created when its
        citekey is not stored yet (a stored one is left as it is)."""
        ids: Dict[str, r.SourceId] = {}
        for key, (citekey, name, description, origin_path) in group.items():
            source_id = self.sources.find(citekey)
            if source_id is None:
                source_id = self.sources.add(r.NewSource(
                    citekey=citekey, name=name, source_type=SOURCE_TYPE, retrieved_date=self.snapshot_date,
                    description=description, origin_path=origin_path))
                self.sources.link_author(source_id, SOURCE_AUTHOR)
            ids[key] = source_id
        return ids


def load_measurements(deps: MeasurementsDeps, *, snapshot_date: str, notes_vault: str,
                      manual_sources: Mapping[str, str], scans: Sequence[Sequence[str]]
                      ) -> measurement_rules.LoadSummary:
    """Insert the vault's readings, in the vault tables' own order, and return what was written. `manual_sources`
    maps a source key to the citekey step 01 seeded; `scans` is [(scan date, source key)] of the DEXA scans."""
    vault, repo = deps.vault, deps.measurements
    run = _Run(deps, snapshot_date)
    inserted_before = repo.totals().measurements

    ids: Dict[str, r.SourceId] = {}
    for name, citekey in manual_sources.items():
        source_id = deps.sources.find(citekey)
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
            repo.add_muscle_volume(r.NewMuscleVolume(muscle_id=muscle_id, week_start=row["date"], sets=row[col],
                                                     source_id=ids["volume"]))

    # ---------------- Raw per-set / per-food / Fitbit / micronutrient logs ----------------
    ids.update(run.create_sources(raw))

    for s in vault.workout_sets():
        repo.add_training_set(r.NewTrainingSet(
            exercise_id=run.exercise_id(s.exercise or "Unknown exercise"), measured_at=s.date, weight=s.completed_weight,
            weight_unit=s.weight_unit or "lb", reps=s.completed_reps, rir=s.rir, is_warmup=1 if s.is_warmup else 0,
            source_id=ids["sets"], notes=s.notes))

    for j in vault.jefit_exercise_sets():
        repo.add_training_set(r.NewTrainingSet(
            exercise_id=run.exercise_id(j.exercise or "Unknown exercise"), measured_at=j.date, weight=j.weight,
            weight_unit=j.weight_unit or "lb", reps=j.reps, rir=None, is_warmup=0, source_id=ids["jefit"], notes=None))

    for row in vault.mfp_measurements():
        run.add_measurements(measurement_rules.mfp_measurements(row), ids)

    for row in vault.mfp_exercise_log():
        run.add_measurements(measurement_rules.exercise_log_measurements(row), ids)

    for meal in vault.mfp_nutrition_log():
        repo.add_meal_log_entry(r.NewMealLogEntry(
            measured_at=meal.date, meal=meal.meal, nutrients={c: getattr(meal, c) for c in MEAL_LOG_NUTRIENTS},
            source_id=ids["mfp_n"]))

    for f in vault.nutrition_food_log():
        repo.add_food_log_entry(r.NewFoodLogEntry(
            food_id=run.food_id(f.food_name or "Unknown food"), measured_at=f.date, time=f.time,
            serving_qty=f.serving_qty, serving_size=f.serving_size, calories_kcal=f.calories_kcal, fat_g=f.fat_g,
            carbs_g=f.carbs_g, protein_g=f.protein_g, alcohol_g=f.alcohol_g, source_id=ids["food"]))

    micro = vault.micronutrients()
    for row in micro.rows:
        run.add_measurements(measurement_rules.micronutrient_measurements(row, micro.columns), ids)

    deps.unit_of_work.commit()
    totals = repo.totals()
    return measurement_rules.LoadSummary(
        inserted=totals.measurements - inserted_before, subjects=len(run.subjects), metrics=len(run.metrics),
        training_sets=totals.training_sets, exercises=len(run.exercises), food_log_entries=totals.food_log_entries,
        foods=len(run.foods), meal_log_entries=totals.meal_log_entries,
        muscle_volume_rows=totals.muscle_volume_weekly, muscles=len(run.muscles))


def link_fact_measurements(deps: FactLinksDeps, items: Iterable[Mapping[str, Any]]) -> int:
    """One `fact_measurements` row per measurement of the metric each loaded fact cites in
    `measurement_metric_link`; returns the number of links made. A fact the facts step skipped is not linked."""
    facts = deps.facts
    linked = 0
    for item in items:
        metric = item.get("measurement_metric_link")
        if not metric:
            continue
        fact_id = facts.find_by_source_key(item["_source_key"])
        if fact_id is None:
            continue   # the facts step skipped this entry (an invalid one), so there is nothing to link
        linked += facts.link_measurements(fact_id, metric)
    deps.unit_of_work.commit()
    return linked
