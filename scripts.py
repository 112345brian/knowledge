"""Composition root for the script-backed commands: binds `scripts_service` to `script_runner`."""
import script_runner
import scripts_service
from wiring import bind_all

API = ("build", "clean_concerts")


def compose():
    """The script-backed commands wired to the real runner."""
    return bind_all(scripts_service, scripts_service.ScriptsDeps(runner=script_runner), API)
