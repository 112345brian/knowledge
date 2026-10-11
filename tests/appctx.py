"""Test helper: a Container for whatever private checkout the test has pointed KNOWLEDGE_PRIVATE_DIR at (or for
explicit locations). Built at call time, so each call sees the current environment and the modules the test has
loaded now (several tests re-import modules, or patch one, and expect the application to use that one)."""
import sys
import threading

_lock = threading.Lock()


def app(locations=None):
    with _lock:   # tests call this from worker threads; importing and re-resolving is not re-entrant
        sys.modules.pop("container", None)
        import container
        return container.Container(locations)


def locs(**overrides):
    """The `Locations` of the private checkout the test has pointed KNOWLEDGE_PRIVATE_DIR at, with any field
    overridden (a vault, a csv). What a pipeline step's run(con, locations) is given."""
    import dataclasses
    import paths
    return dataclasses.replace(paths.load(), **overrides)
