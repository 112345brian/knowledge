"""The plain values of the measurements source's record-shaped repository contract
(`measurements_port.MeasurementRepository`).

Client-owned: these used to sit in the core's `build_records` beside the sources and facts records, which made the
core's contract name a client source. They keep their fields and meaning; only the owner changed. A client module may
import core ids (`build_records.SubjectId`, `SourceId`); the core never imports this module.

Domain service ring: frozen dataclasses, no I/O.
"""
from dataclasses import dataclass
from typing import Mapping, NewType, Optional

from build_records import SourceId, SubjectId

MetricId = NewType("MetricId", int)


@dataclass(frozen=True)
class NewMetric:
    key: str
    label: str
    unit: Optional[str]
    good_direction: Optional[int]       # metrics.good_direction is INTEGER: 1 up is favorable, -1 down, 0/NULL neutral


@dataclass(frozen=True)
class NewMeasurement:
    subject_id: SubjectId
    metric_id: MetricId
    value: object
    measured_at: str
    source_id: SourceId
    trust_level: str
    trust_rationale: Optional[str]
    date_added: str
    notes: Optional[str] = None
    recheck_by: Optional[str] = None
    recheck_rationale: Optional[str] = None


@dataclass(frozen=True)
class NewTrainingSet:
    exercise_id: int
    measured_at: str
    weight: Optional[float]
    weight_unit: str
    reps: Optional[int]
    rir: Optional[float]
    is_warmup: int
    source_id: SourceId
    notes: Optional[str] = None


@dataclass(frozen=True)
class NewFoodLogEntry:
    food_id: int
    measured_at: str
    time: Optional[str]
    serving_qty: Optional[float]
    serving_size: Optional[str]
    calories_kcal: Optional[float]
    fat_g: Optional[float]
    carbs_g: Optional[float]
    protein_g: Optional[float]
    alcohol_g: Optional[float]
    source_id: SourceId


@dataclass(frozen=True)
class NewMealLogEntry:
    measured_at: str
    meal: Optional[str]
    nutrients: Mapping[str, Optional[float]]    # calories_kcal, fat_g, saturated_fat_g, carbs_g, fiber_g, sugar_g,
    source_id: SourceId                         # protein_g, sodium_mg, potassium_mg, cholesterol_mg, vitamin_a,
    # vitamin_c, calcium, iron: the columns of meal_log_entries, by name


@dataclass(frozen=True)
class NewMuscleVolume:
    muscle_id: int
    week_start: str
    sets: object
    source_id: SourceId


@dataclass(frozen=True)
class MeasurementTotals:
    """The table sizes the measurements step reports."""
    measurements: int
    training_sets: int
    food_log_entries: int
    meal_log_entries: int
    muscle_volume_weekly: int
