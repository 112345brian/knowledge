"""The snapshot date of the vault-db measurements load (issue #35): the adapter that reads
`measurements_snapshot.json` from the private data dir. Validation and the error messages are the domain's
(`measurement_rules`). A missing or malformed file fails the build loudly."""
import os

from client import measurement_rules
from client.measurement_rules import MEASUREMENTS_SNAPSHOT_FILE  # noqa: F401  (the public API)


def read_snapshot_date(data_dir):
    path = os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise RuntimeError(measurement_rules.missing_snapshot_message(path)) from None
    return measurement_rules.parse_snapshot_text(text, path)
