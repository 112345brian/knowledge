"""Composition root for the script-backed commands: binds `scripts_service` to `script_runner`."""
import script_runner
import scripts_service
from wiring import bind

DEPS = scripts_service.ScriptsDeps(runner=script_runner)

build = bind(scripts_service.build, DEPS)
clean_concerts = bind(scripts_service.clean_concerts, DEPS)
