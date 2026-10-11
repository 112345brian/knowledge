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


def synthetic_locations():
    """Locations for a test that has no private checkout of its own: real-looking, absolute, under a per-process
    scratch directory nothing else uses. Tests that touch the disk pass explicit paths or use their own layout."""
    import os
    import tempfile
    from locations import Locations
    root = os.path.join(tempfile.gettempdir(), f"knowledge-test-synthetic-{os.getpid()}")
    return Locations(knowledge_db_dir=os.path.join(root, "db"), private_data_dir=os.path.join(root, "data"),
                     notes_vault=os.path.join(root, "vault"), health_dir=os.path.join(root, "health"))


def _default_locations():
    """The locations of a private checkout the test itself pointed KNOWLEDGE_PRIVATE_DIR at, else synthetic ones.
    (conftest removes any ambient value before every test, so a developer's real checkout is never read.)"""
    import paths
    try:
        return paths.load()
    except ModuleNotFoundError:
        return synthetic_locations()


def app(locations=None, clock=None):
    import clock as clocks
    with _lock:   # tests call this from worker threads; importing and re-resolving is not re-entrant
        sys.modules.pop("container", None)
        import container
        return container.Container(_default_locations() if locations is None else locations, clock)


def locs(**overrides):
    """The StepContext a pipeline step's run(con, ctx) is given: the `Locations` of the private checkout the test
    has pointed KNOWLEDGE_PRIVATE_DIR at (any field overridden: a vault, a csv) and the real local inputs."""
    import dataclasses
    import local_inputs
    import local_stores
    from pipeline_context import StepContext
    return StepContext(dataclasses.replace(_default_locations(), **overrides), local_inputs, local_stores)
