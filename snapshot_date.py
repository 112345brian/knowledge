"""The snapshot date of the vault-db measurements load (issue #35).

The vault's bodybuilding.db rows carry the date a reading was *taken* but no timestamp for when
it was loaded into knowledge.db, so there is no real per-row "date added" to use. The date the
snapshot was taken lives in the private data (`measurements_snapshot.json`, written once by
backfill_dates.py), never in code. A missing or malformed file fails the build loudly.
"""
import json
import os
from datetime import datetime

MEASUREMENTS_SNAPSHOT_FILE = "measurements_snapshot.json"


def read_snapshot_date(data_dir):
    path = os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"{path} is missing: the measurements snapshot date is not stored in the data yet. "
            f"Run backfill_dates.py --apply (see the README, 'Dates'); a date is never invented.") from None
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is not valid JSON ({e})") from e
    value = data.get("synced_at") if isinstance(data, dict) else None
    try:
        if not isinstance(value, str):
            raise ValueError
        datetime.fromisoformat(value)
    except ValueError:
        raise RuntimeError(f"{path}: `synced_at` must be an ISO date or timestamp string, got {value!r}") from None
    return value
