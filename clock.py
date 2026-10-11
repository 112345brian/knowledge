"""The system's clocks: where `Clock` (clock_port) is implemented.

`SystemClock` reads the wall clock. `FixedClock` always answers the same instant (tests, and a subprocess started
with KNOWLEDGE_FROZEN_NOW set). Nothing here is ambient: an entry point picks one with `from_environment()` and
hands it down; code that needs the time is given a `Clock`.

`now_iso()` returns an ISO-8601 UTC timestamp with an explicit offset, e.g. `2026-10-03T12:34:56+00:00`,
truncated to whole seconds, so "what did I believe on date X" has one consistent source. A fixed value must
carry an offset (naive times are rejected) and is normalised to UTC.
"""
import os
from datetime import datetime, timezone

ENV_VAR = "KNOWLEDGE_FROZEN_NOW"


def parse(value):
    """A datetime (or ISO-8601 string) with an offset, as UTC; ValueError for a naive or unparseable value."""
    text = value if isinstance(value, str) else value.isoformat()
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"fixed time {text!r} has no UTC offset (use e.g. 2026-10-03T08:00:00+00:00)")
    return dt.astimezone(timezone.utc)


class SystemClock:
    def now(self):
        """Current time as a timezone-aware UTC datetime."""
        return datetime.now(timezone.utc)

    def now_iso(self):
        return self.now().replace(microsecond=0).isoformat()


class FixedClock(SystemClock):
    def __init__(self, value):
        self._now = parse(value)

    def now(self):
        return self._now


def from_environment(environ=None):
    """A FixedClock when KNOWLEDGE_FROZEN_NOW is set (a bad value is an error, never ignored), else the system clock."""
    environ = os.environ if environ is None else environ
    value = environ.get(ENV_VAR)
    return FixedClock(value) if value else SystemClock()
