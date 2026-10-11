"""Test helpers: a Container for whatever private checkout the test has pointed KNOWLEDGE_PRIVATE_DIR at (or for
explicit locations), `locs()` for what a pipeline step is given, and `frozen()` to fix the time the Containers
made inside it use. Built at call time, so each call sees the current environment and the modules the test has
loaded now (several tests re-import modules, or patch one, and expect the application to use that one)."""
import contextlib
import contextvars
import sys
import threading

_lock = threading.Lock()
_frozen = contextvars.ContextVar("test_frozen_now", default=None)


@contextlib.contextmanager
def frozen(value):
    """Containers made by app() inside this block use a FixedClock at `value` (an ISO string or datetime with an
    offset). A test-only convenience: the application itself has no ambient time."""
    import clock
    clock.parse(value)            # reject a naive or unparseable value here, where the test is
    token = _frozen.set(value)
    try:
        yield
    finally:
        _frozen.reset(token)


def fixed_clock(value):
    import clock
    return clock.FixedClock(value)


def system_clock():
    import clock
    return clock.SystemClock()


def app(locations=None, clock=None):
    import clock as clocks
    with _lock:   # tests call this from worker threads; importing and re-resolving is not re-entrant
        sys.modules.pop("container", None)
        import container
        if clock is None and _frozen.get() is not None:
            clock = clocks.FixedClock(_frozen.get())
        return container.Container(locations, clock)


def locs(**overrides):
    """The `Locations` of the private checkout the test has pointed KNOWLEDGE_PRIVATE_DIR at, with any field
    overridden (a vault, a csv). What a pipeline step's run(con, locations) is given."""
    import dataclasses
    import paths
    return dataclasses.replace(paths.load(), **overrides)
