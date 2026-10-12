"""Port for what the measurements load writes into the knowledge database: the metrics, measurements, exercises,
foods, muscles and the training, food and meal log rows of the `measurements` client source. Client-owned: the core's
build contract does not know them.

One contract, record-shaped (`measurements_records`); the service maps a vault reading to the records the repository
stores and commits through the build's unit of work, the adapter (`measurements_store`) owns the SQL and decides
nothing. The fact <-> measurement link is the link step's own narrow port (`measurements_service.FactLinks`).
"""
from typing import Optional, Protocol

from client import measurements_records as m


class MeasurementRepository(Protocol):
    def find_metric(self, key: str) -> Optional[m.MetricId]: ...
    def add_metric(self, metric: m.NewMetric) -> m.MetricId: ...
    def add(self, measurement: m.NewMeasurement) -> None: ...
    def exercise(self, name: str) -> int:
        """Get-or-create."""
    def food(self, name: str) -> int:
        """Get-or-create."""
    def muscle(self, name: str) -> int:
        """Get-or-create."""
    def add_training_set(self, training_set: m.NewTrainingSet) -> None: ...
    def add_food_log_entry(self, entry: m.NewFoodLogEntry) -> None: ...
    def add_meal_log_entry(self, entry: m.NewMealLogEntry) -> None: ...
    def add_muscle_volume(self, volume: m.NewMuscleVolume) -> None: ...
    def totals(self) -> m.MeasurementTotals: ...
