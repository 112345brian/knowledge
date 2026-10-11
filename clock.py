"""The one place that reads the wall clock for anything written into the data.

`now_iso()` returns an ISO-8601 UTC timestamp with an explicit offset, e.g.
`2026-10-03T12:34:56+00:00`, truncated to whole seconds. add_fact stamps
`date_added` / `captured_at` with it, and the revision log (#30) is meant to use
the same helper, so "what did I believe on date X" has one consistent source.

Tests freeze it with `with clock.frozen("2026-10-03T08:00:00+00:00"): ...`, or,
for a subprocess, by setting the KNOWLEDGE_FROZEN_NOW environment variable to
the same kind of string. A frozen value must carry an offset (naive times are
rejected) and is normalised to UTC.
"""
import contextlib
import contextvars
import os
from datetime import datetime, timezone

ENV_VAR = "KNOWLEDGE_FROZEN_NOW"
# The one ambient value in the system, and only a test seam: a context variable (scoped, restored on exit),
# not a module global that a `global` statement rebinds.
_frozen = contextvars.ContextVar("knowledge_frozen_now", default=None)


def _parse(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"frozen time {value!r} has no UTC offset (use e.g. 2026-10-03T08:00:00+00:00)")
    return dt.astimezone(timezone.utc)


def now():
    """Current time as a timezone-aware UTC datetime (frozen value if one is set)."""
    frozen_now = _frozen.get()
    if frozen_now is not None:
        return frozen_now
    env = os.environ.get(ENV_VAR)
    if env:
        return _parse(env)
    return datetime.now(timezone.utc)


def now_iso():
    return now().replace(microsecond=0).isoformat()


@contextlib.contextmanager
def frozen(value):
    """Freeze `now()` to an ISO-8601 string with an offset, or a datetime that has one."""
    dt = _parse(value) if isinstance(value, str) else _parse(value.isoformat())
    token = _frozen.set(dt)
    try:
        yield
    finally:
        _frozen.reset(token)
