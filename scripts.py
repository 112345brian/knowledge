"""Composition root for the script-backed commands: binds `scripts_service` to `script_runner`.

Composition ring (tach layer `composition`): the one place that picks the real `script_runner` adapter for `scripts_service`."""
import script_runner
import scripts_service
from wiring import bind_all

API = ("build", "run_tool")


def compose(locations, clock):
    """The script-backed commands wired to the real runner, for these `locations` and this `clock`."""
    deps = scripts_service.ScriptsDeps(runner=script_runner, clock=clock, locations=locations)
    return bind_all(scripts_service, deps, API)
