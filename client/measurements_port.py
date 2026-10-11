"""Ports for what the measurements load writes into the knowledge database.

`MeasurementRepository` is the measurement/metric/exercise/food/muscle writes of the `measurements` client
source; `FactMeasurementLinks` is the fact <-> measurement link of the step that runs after the facts step.
Neither caches: `get_or_create_*` is a lookup-or-insert on every call, and the per-run caches that avoid the
repeated lookups belong to the service for the length of one run.
"""
from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class MeasurementRecord:
    """One `measurements` row, its subject and metric already resolved to ids."""
    subject_id: int
    metric_id: int
    value: Any
    measured_at: str
    source_id: int
    trust_level: str
    trust_rationale: Optional[str]
    date_added: str
    notes: Optional[str] = None
    recheck_by: Optional[str] = None
    recheck_rationale: Optional[str] = None


@dataclass(frozen=True)
class TrainingSetRecord:
    exercise_id: int
    measured_at: Any
    weight: Any
    weight_unit: str
    reps: Any
    rir: Any
    is_warmup: int
    source_id: int
    notes: Optional[str] = None


@dataclass(frozen=True)
class MuscleVolumeRecord:
    muscle_id: int
    week_start: Any
    sets: Any
    source_id: int


@dataclass(frozen=True)
class MealLogRecord:
    """One `meal_log_entries` row (the macro and micro totals of one logged meal)."""
    measured_at: Any
    meal: Any
    calories_kcal: Any
    fat_g: Any
    saturated_fat_g: Any
    carbs_g: Any
    fiber_g: Any
    sugar_g: Any
    protein_g: Any
    sodium_mg: Any
    potassium_mg: Any
    cholesterol_mg: Any
    vitamin_a: Any
    vitamin_c: Any
    calcium: Any
    iron: Any
    source_id: int


@dataclass(frozen=True)
class FoodLogRecord:
    food_id: int
    measured_at: Any
    time: Any
    serving_qty: Any
    serving_size: Any
    calories_kcal: Any
    fat_g: Any
    carbs_g: Any
    protein_g: Any
    alcohol_g: Any
    source_id: int


@dataclass(frozen=True)
class TableCounts:
    """Rows in each table the load writes, whole-table (what earlier steps put there counts too)."""
    measurements: int
    training_sets: int
    food_log_entries: int
    meal_log_entries: int
    muscle_volume_weekly: int


class MeasurementRepository(Protocol):
    def source_id(self, citekey: str) -> Optional[int]: ...
    def get_or_create_source(self, citekey: str, name: str, description: str, origin_path: Optional[str],
                             retrieved_date: str, author: str) -> int: ...
    def get_or_create_subject(self, name: str) -> int: ...
    def get_or_create_metric(self, key: str, unit: str, label: str, good_direction: Optional[int]) -> int: ...
    def get_or_create_exercise(self, name: str) -> int: ...
    def get_or_create_food(self, name: str) -> int: ...
    def get_or_create_muscle(self, name: str) -> int: ...
    def add_measurement(self, record: MeasurementRecord) -> None: ...
    def add_training_set(self, record: TrainingSetRecord) -> None: ...
    def add_muscle_volume(self, record: MuscleVolumeRecord) -> None: ...
    def add_meal_log_entry(self, record: MealLogRecord) -> None: ...
    def add_food_log_entry(self, record: FoodLogRecord) -> None: ...
    def counts(self) -> TableCounts: ...
    def commit(self) -> None: ...


class FactMeasurementLinks(Protocol):
    def fact_id(self, source_key: str) -> Optional[int]: ...
    def link_metric(self, fact_id: int, metric_key: str) -> int: ...
    def commit(self) -> None: ...
