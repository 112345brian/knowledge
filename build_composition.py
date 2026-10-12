"""Composition root of the build: opens a session and binds the ordered pipeline to it.

Composition ring: it picks the adapters and hands each step only what it declares; it holds no rules and no SQL.
The two things it owns:

    `BuildSessionFactory`   `open(target)` is a context manager yielding a `BuildSession` (the repositories and
                            unit of work over one build database) from a `SessionOpener`, an infrastructure
                            object (the sqlite adapter a lane or the integration owner adds) that knows how to
                            connect, apply the schema and close.
    `PipelineBinder`        a registry `path -> StepFactory`; `bind(session, paths, environment)` returns the
                            `BoundStep`s for the paths it is given, in that order. The order comes from
                            `build_rules.steps_for(sources)`: the pipeline ring passes it in (this ring may not
                            import it), so the ingest order keeps exactly one definition.

`run_pipeline` runs bound steps in order, emits each result's lines, and wraps a failure as `StepError(path, cause)`
(only `Exception`, like build.py's loop: a KeyboardInterrupt is not a step failure).

A step that is still a legacy `run(con, ctx)` module has no registry entry; `unbound(paths)` lists them and `bind`
refuses to build a partial pipeline silently (`UnboundSteps`), so a cutover is a deliberate act.
"""
from contextlib import contextmanager
from typing import Callable, ContextManager, Dict, Iterator, List, Mapping, Protocol, Sequence

from build_repositories_port import BuildSession
from build_steps_port import BoundStep, StepEnvironment, StepError, StepFactory, StepResult


class SessionOpener(Protocol):
    """Infrastructure: connect to the build database at `target`, apply what it needs, yield the session, and
    close (discarding anything uncommitted) when the block ends, normally or not."""
    def open(self, target: str) -> ContextManager[BuildSession]: ...


class UnboundSteps(Exception):
    """The pipeline names steps that have no bound implementation."""
    def __init__(self, paths: Sequence[str]):
        super().__init__(f"no bound implementation for step(s): {list(paths)}")
        self.paths = tuple(paths)


class BuildSessionFactory:
    def __init__(self, opener: SessionOpener):
        self._opener = opener

    @contextmanager
    def open(self, target: str) -> Iterator[BuildSession]:
        with self._opener.open(target) as session:
            yield session


class PipelineBinder:
    def __init__(self, registry: Mapping[str, StepFactory]):
        self._registry: Dict[str, StepFactory] = dict(registry)

    def unbound(self, paths: Sequence[str]) -> List[str]:
        return [p for p in paths if p not in self._registry]

    def bind(self, session: BuildSession, paths: Sequence[str], environment: StepEnvironment) -> List[BoundStep]:
        missing = self.unbound(paths)
        if missing:
            raise UnboundSteps(missing)
        return [BoundStep(p, self._registry[p](session, environment)) for p in paths]


def run_pipeline(steps: Sequence[BoundStep], emit: Callable[[str], None]) -> List[StepResult]:
    """Run each step, in order, emitting its result lines after it returns."""
    results = []
    for bound in steps:
        try:
            result = bound.step.run()
        except Exception as e:
            raise StepError(bound.path, e) from e
        for line in result.lines:
            emit(line)
        results.append(result)
    return results
