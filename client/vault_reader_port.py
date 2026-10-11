"""Port for reading the vault's own database (the health/fitness vault's `bodybuilding.db`), as typed records.

The measurements load reads the vault only through this protocol, so a test can hand it an in-memory vault and the
service holds no sqlite. Every method is one table scan in the table's own order (the vault tables have no
`ORDER BY`; the scan order is the build's insertion order and is part of what the build pins).

Rows the pure rules consume are `VaultRow` mappings keyed by the vault's column names (the rules name the columns
they read). Rows the load consumes itself are the dataclasses below. A table whose columns are discovered from
the vault (muscle volume, micronutrients) is a `ColumnTable`: its metric columns come from the table's own
definition, so they are known even when the table has no rows.
"""
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol, Tuple

VaultRow = Mapping[str, Any]


@dataclass(frozen=True)
class WorkoutSet:
    date: Any
    exercise: Optional[str]
    completed_weight: Any
    weight_unit: Optional[str]
    completed_reps: Any
    rir: Any
    is_warmup: Any
    notes: Optional[str]


@dataclass(frozen=True)
class JefitSet:
    date: Any
    exercise: Optional[str]
    weight: Any
    weight_unit: Optional[str]
    reps: Any


@dataclass(frozen=True)
class MealLog:
    date: Any
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


@dataclass(frozen=True)
class FoodLog:
    date: Any
    time: Any
    food_name: Optional[str]
    serving_qty: Any
    serving_size: Any
    calories_kcal: Any
    fat_g: Any
    carbs_g: Any
    protein_g: Any
    alcohol_g: Any


@dataclass(frozen=True)
class ColumnTable:
    """A vault table with one column per metric: `columns` are the metric columns in the table's column order
    (the date and any bookkeeping column excluded), `rows` carry `date` plus a value for each of them."""
    columns: Tuple[str, ...]
    rows: Iterable[VaultRow]


class VaultReader(Protocol):
    def dexa_scan(self, scan_date: str) -> Optional[VaultRow]: ...
    def bloodwork(self) -> Iterable[VaultRow]: ...
    def body_measurements(self) -> Iterable[VaultRow]: ...
    def latest_strength_checkpoint(self) -> Optional[VaultRow]: ...
    def nutrition_daily(self) -> Iterable[VaultRow]: ...
    def scale_readings(self) -> Iterable[VaultRow]: ...
    def renpho_scale_readings(self) -> Iterable[VaultRow]: ...
    def muscle_volume_weekly(self) -> ColumnTable: ...
    def workout_sets(self) -> Iterable[WorkoutSet]: ...
    def jefit_exercise_sets(self) -> Iterable[JefitSet]: ...
    def mfp_measurements(self) -> Iterable[VaultRow]: ...
    def mfp_exercise_log(self) -> Iterable[VaultRow]: ...
    def mfp_nutrition_log(self) -> Iterable[MealLog]: ...
    def nutrition_food_log(self) -> Iterable[FoodLog]: ...
    def micronutrients(self) -> ColumnTable: ...
    def close(self) -> None: ...
