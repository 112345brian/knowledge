"""What a source's acquisition record may hold (when, how, from where, a note): the vocabulary and the validation
of the data-supplied fields, free of how file attributes are read.

Model ring: imports only `validtime`, and does no I/O. `acquisition_store` (the adapter that reads macOS file
attributes) and the build steps share these names.
"""
from datetime import datetime

import validtime

VIA_VALUES = ("download", "manual", "export", "unknown")  # keep in sync with the CHECKs in schema.sql
COLUMNS = ("acquired_at", "acquired_via", "where_from", "acquired_note")


class AcquisitionError(Exception):
    """A value in the data is invalid. The message names the source."""


def _text(value, where, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise AcquisitionError(f"{where}: {field} must be text, not {type(value).__name__}")
    return value.strip() or None


def _valid_when(value):
    if validtime.is_valid_boundary(value):
        return True
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


def normalize_data(raw, where):
    """The data-supplied acquisition fields {acquired_at, acquired_via, where_from, acquired_note}: blank -> None,
    validated. Raises AcquisitionError naming `where`."""
    out = {k: _text(raw.get(k), where, k) for k in COLUMNS}
    if out["acquired_at"] is not None and not _valid_when(out["acquired_at"]):
        raise AcquisitionError(f"{where}: acquired_at {out['acquired_at']!r} must be an ISO date (YYYY, YYYY-MM, YYYY-MM-DD) or timestamp")
    if out["acquired_via"] is not None and out["acquired_via"] not in VIA_VALUES:
        raise AcquisitionError(f"{where}: acquired_via {out['acquired_via']!r} must be one of {list(VIA_VALUES)}")
    return out
