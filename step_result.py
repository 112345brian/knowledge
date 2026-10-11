"""What an ingest step reports back: a value, no I/O.

Model ring. A step used to print as it went and return nothing, so a test could only see what it did by capturing
stdout or reading the database. A service now returns a `StepResult`; the step module (a driving adapter) prints
`lines` and the build carries on. `counts` is the same information as numbers, for a test or an audit.

`skipped` means the step found nothing to do (an optional input is absent): it wrote no rows and committed nothing.
"""
from dataclasses import dataclass, field
from typing import Mapping, Tuple


@dataclass(frozen=True)
class StepResult:
    step: str
    counts: Mapping[str, int] = field(default_factory=dict)
    lines: Tuple[str, ...] = ()
    skipped: bool = False
