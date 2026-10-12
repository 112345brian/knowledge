"""The shape of a build step once it is bound to its dependencies: `run() -> StepResult`.

Domain service ring: no I/O. Today a step is a module function `run(con, ctx)` that prints as it goes. A bound step
instead holds only the repositories and inputs it declares (its own frozen `*Deps` dataclass, built by the
composition root's registry from the session) and returns what it did as a `StepResult`; the pipeline runner is the
only thing that prints.

Output contract (tests/test_build_characterization.py pins today's text per step):

    * a warning raised while the step works goes to `Diagnostics.emit` at once, exactly where today's `print` is
      (so a step that fails later has still shown what it saw before failing);
    * everything printed after the step's last write (privacy-raised note, the `[step] ...` summary line, the
      indented detail lines) is `StepResult.lines`, in order; the runner emits them after `run()` returns.
"""
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence, Tuple

import inputs_port
from build_repositories_port import BuildSession
from locations import Locations


class Diagnostics(Protocol):
    def emit(self, line: str) -> None:
        """One line of output, shown now (production: print; tests: collect)."""


@dataclass(frozen=True)
class StepResult:
    """What a step did. `lines` are the output lines in print order and contain exactly one summary line that
    starts with `[<step>] ` (the `[step] ...` line every step prints today); `counts` are the named tallies the
    lines are built from, so a lane can assert numbers without parsing text. `skipped` is True for the branches
    that return before doing the step's work (no subjects.json, scrobbles file not found, no entities.json)."""
    step: str
    lines: Tuple[str, ...]
    counts: Mapping[str, int]
    skipped: bool = False

    def __post_init__(self):
        headline = [ln for ln in self.lines if ln.startswith(f"[{self.step}] ")]
        if len(headline) != 1:
            raise ValueError(f"a StepResult for {self.step!r} needs exactly one '[{self.step}] ' line, got {len(headline)}")

    @property
    def headline(self) -> str:
        return next(ln for ln in self.lines if ln.startswith(f"[{self.step}] "))


class BuildStep(Protocol):
    """A step bound to its dependencies. `name` is the module's short name (`seed_sources`), the one in
    `[name]` output lines."""
    name: str

    def run(self) -> StepResult: ...


class StepError(Exception):
    """A step failed; `stage` is its path in the pipeline (`ingest/facts.py`), `cause` the exception.
    Same two attributes and message shape as `build.BuildError`, which the build driver maps this to."""
    def __init__(self, stage: str, cause: BaseException):
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause


@dataclass(frozen=True)
class StepEnvironment:
    """What the registry hands every step factory besides the session: where the data lives, what a step may read
    from outside the database (inputs_port.Inputs, and the vault reader for the one step that needs it), and
    where its warnings go."""
    locations: Locations
    inputs: inputs_port.Inputs
    diagnostics: Diagnostics


StepFactory = Callable[[BuildSession, StepEnvironment], BuildStep]


@dataclass(frozen=True)
class BoundStep:
    """A bound step and the pipeline path it was bound for (the `stage` of a failure)."""
    path: str
    step: BuildStep

