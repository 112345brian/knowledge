"""SQLite adapter for `vault_reader_port.VaultReader`: the vault's own database, read table by table.

Given an open connection to the vault's database (opened through the inputs port, so the file is never touched
here). Every scan is `SELECT *` in the table's own order, as the vault stores it; only the latest strength
checkpoint is ordered. Rows come back as plain dicts keyed by the vault's column names.
"""
import sqlite3
from typing import Iterator, Optional

from client.vault_reader_port import ColumnTable, FoodLog, JefitSet, MealLog, VaultRow, WorkoutSet

DATE_AND_BOOKKEEPING = {
    "muscle_volume_weekly": ("date",),
    "micronutrients": ("date", "source_file"),
}


class SqliteVaultReader:
    def __init__(self, connection):
        connection.row_factory = sqlite3.Row
        self._con = connection

    def _scan(self, table: str):
        return self._con.execute(f"SELECT * FROM {table}")

    def _rows(self, table: str) -> Iterator[VaultRow]:
        for row in self._scan(table):
            yield dict(row)

    def _column_table(self, table: str) -> ColumnTable:
        skip = DATE_AND_BOOKKEEPING[table]
        columns = tuple(d[0] for d in self._con.execute(f"SELECT * FROM {table} LIMIT 1").description
                        if d[0] not in skip)
        return ColumnTable(columns, self._rows(table))

    def dexa_scan(self, scan_date: str) -> Optional[VaultRow]:
        row = self._con.execute("SELECT * FROM bodycomp_dexa WHERE date = ?", (scan_date,)).fetchone()
        return dict(row) if row is not None else None

    def bloodwork(self):
        return self._rows("bloodwork")

    def body_measurements(self):
        return self._rows("body_measurements")

    def latest_strength_checkpoint(self) -> Optional[VaultRow]:
        row = self._con.execute("SELECT * FROM strength_checkpoints ORDER BY date DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def nutrition_daily(self):
        return self._rows("nutrition_daily")

    def scale_readings(self):
        return self._rows("scale_readings")

    def renpho_scale_readings(self):
        return self._rows("renpho_scale_readings")

    def muscle_volume_weekly(self) -> ColumnTable:
        return self._column_table("muscle_volume_weekly")

    def workout_sets(self):
        for r in self._scan("workout_sets"):
            yield WorkoutSet(date=r["date"], exercise=r["exercise"], completed_weight=r["completed_weight"],
                             weight_unit=r["weight_unit"], completed_reps=r["completed_reps"], rir=r["rir"],
                             is_warmup=r["is_warmup"], notes=r["notes"])

    def jefit_exercise_sets(self):
        for r in self._scan("jefit_exercise_sets"):
            yield JefitSet(date=r["date"], exercise=r["exercise"], weight=r["weight"], weight_unit=r["weight_unit"],
                           reps=r["reps"])

    def mfp_measurements(self):
        return self._rows("mfp_measurements")

    def mfp_exercise_log(self):
        return self._rows("mfp_exercise_log")

    def mfp_nutrition_log(self):
        for r in self._scan("mfp_nutrition_log"):
            yield MealLog(date=r["date"], meal=r["meal"], calories_kcal=r["calories_kcal"], fat_g=r["fat_g"],
                          saturated_fat_g=r["saturated_fat_g"], carbs_g=r["carbs_g"], fiber_g=r["fiber_g"],
                          sugar_g=r["sugar_g"], protein_g=r["protein_g"], sodium_mg=r["sodium_mg"],
                          potassium_mg=r["potassium_mg"], cholesterol_mg=r["cholesterol_mg"],
                          vitamin_a=r["vitamin_a"], vitamin_c=r["vitamin_c"], calcium=r["calcium"], iron=r["iron"])

    def nutrition_food_log(self):
        for r in self._scan("nutrition_food_log"):
            yield FoodLog(date=r["date"], time=r["time"], food_name=r["food_name"], serving_qty=r["serving_qty"],
                          serving_size=r["serving_size"], calories_kcal=r["calories_kcal"], fat_g=r["fat_g"],
                          carbs_g=r["carbs_g"], protein_g=r["protein_g"], alcohol_g=r["alcohol_g"])

    def micronutrients(self) -> ColumnTable:
        return self._column_table("micronutrients")

    def close(self) -> None:
        self._con.close()
