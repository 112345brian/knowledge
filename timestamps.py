"""Pure timestamp parsing for revision times (domain: no clock, no I/O).

`parse_timestamp` turns an ISO date or timestamp into an aware UTC datetime; `parse_as_of` turns an
"as of" argument into the cutoff instant. `revisions` (storage) and `modes` (visibility policy) both
need them, and neither may pull the other in, so they live here.
"""
import re
from datetime import datetime, timedelta, timezone


def parse_timestamp(value):
    """ISO date or timestamp -> aware UTC datetime. A bare date is midnight UTC."""
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a string")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def has_offset(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def parse_as_of(value):
    """'YYYY-MM-DD' means the end of that day (UTC); a full timestamp is taken as is."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return parse_timestamp(value) + timedelta(days=1) - timedelta(microseconds=1)
    try:
        return parse_timestamp(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"as-of {value!r} must be YYYY-MM-DD or an ISO-8601 timestamp") from e
