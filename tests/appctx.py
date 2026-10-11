"""Test helpers: a Container for whatever private checkout the test has pointed KNOWLEDGE_PRIVATE_DIR at (or for
explicit locations and an explicit clock: `app(clock=fixed_clock(...))` fixes the time) and `locs()` for what a
pipeline step is given. Nothing here is ambient. Built at call time, so each call sees the current environment and the modules the test has
loaded now (several tests re-import modules, or patch one, and expect the application to use that one)."""
import sys
import threading

_lock = threading.Lock()


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
        return container.Container(locations, clock)


def locs(**overrides):
    """The StepContext a pipeline step's run(con, ctx) is given: the `Locations` of the private checkout the test
    has pointed KNOWLEDGE_PRIVATE_DIR at (any field overridden: a vault, a csv) and the real local inputs."""
    import dataclasses
    import local_inputs
    import local_stores
    import paths
    from pipeline_context import StepContext
    return StepContext(dataclasses.replace(paths.load(), **overrides), local_inputs, local_stores)
