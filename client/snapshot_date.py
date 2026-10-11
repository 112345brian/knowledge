"""The private-data inputs of the vault-db measurements load: the adapter that reads `measurements_snapshot.json`
(the snapshot date, issue #35) and `measurements_config.json` from the private data dir. Validation and the error
messages are the domain's (`measurement_rules`). A missing or malformed file fails the build loudly."""
import json
import os

from client import measurement_rules
from client.measurement_rules import MEASUREMENTS_SNAPSHOT_FILE  # noqa: F401  (the public API)


def read_snapshot_date(inputs, data_dir):
    path = os.path.join(data_dir, MEASUREMENTS_SNAPSHOT_FILE)
    try:
        text = inputs.read_text(path)
    except FileNotFoundError:
        raise RuntimeError(measurement_rules.missing_snapshot_message(path)) from None
    return measurement_rules.parse_snapshot_text(text, path)


def read_config(inputs, data_dir):
    """The source identifiers and selected record dates of the measurements load, validated."""
    path = os.path.join(data_dir, measurement_rules.MEASUREMENTS_CONFIG_FILE)
    try:
        config = inputs.read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"{path}: cannot read measurement configuration: {e}") from e
    return measurement_rules.validate_config(config, path)
