"""Ingest structured, repeatable numeric readings, lab results, manual
measurements, activity summaries, and other local source records into the database.
into `measurements` (subject_id/metric_id resolved via get-or-create
helpers, never written as repeated text). Per-set training, per-food, and
per-meal logs go into their own event tables instead (`training_sets`,
`food_log_entries`, `meal_log_entries`) with `exercises`/`foods` as proper
entity tables -- a set or a food-log line is one event with several
co-occurring attributes, not independent measurements sharing a date.
Run after seed_sources.py (needs its citekeys to exist).

Input: a local structured database configured by the private companion.

This step is the driving adapter: it reads the snapshot date and the configuration, opens the vault, binds the
SQLite adapters to the load use case (`measurements_service`) and prints its summary. The vault db has no per-row
load timestamp (only the date each reading was taken), so the date_added of these rows, and the retrieved_date of
the vault-db sources, is the date the snapshot was taken. It is read from the data (measurements_snapshot.json,
written once by backfill_snapshot_date.py); a missing or malformed file fails the build. #35.
"""
import os

from client import measurement_rules, measurements_service, measurements_store, snapshot_date, vault_reader_store
from locations import join_path


def run(con, ctx):
    date = snapshot_date.read_snapshot_date(ctx.inputs, ctx.locations.private_data_dir)  # raises, naming the file, if it is not in the data
    notes_vault = ctx.locations.need("notes_vault", "NOTES_VAULT", "the measurements source")
    vault = vault_reader_store.SqliteVaultReader(ctx.inputs.open_database(join_path(notes_vault, "bodybuilding.db")))
    try:
        config = snapshot_date.read_config(ctx.inputs, ctx.locations.private_data_dir)
        deps = measurements_service.MeasurementsDeps(vault=vault, repository=measurements_store.SqliteMeasurementRepository(con))
        summary = measurements_service.load_measurements(
            deps, snapshot_date=date, notes_vault=ctx.locations.notes_vault_stored, manual_sources=config["manual_sources"],
            scans=config["scans"])
    finally:
        vault.close()
    for line in measurement_rules.summary_lines(summary):
        print(line)
