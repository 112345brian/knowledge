"""Composition root for the script-backed commands: binds `scripts_service` to `script_runner`."""
import script_runner
import scripts_service
from wiring import bind_all

API = ("build", "clean_concerts")


def compose(locations, clock):
    """The script-backed commands wired to the real runner, for these `locations` and this `clock`."""
    deps = scripts_service.ScriptsDeps(runner=script_runner, clock=clock, locations=locations)
    return bind_all(scripts_service, deps, API)
