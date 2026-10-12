"""Ports of the database rebuild that are not the file system (`build_files_port` is that one): the session a
build writes through, the registry that runs a step in it, the provenance a build records about itself, and the
outputs that follow a build (the table-count report, knowledge-normal.db).

`build_service` decides the order, the failure naming and when each of these is called; the adapters live in the
composition root (`building`: session, registry), `build_info_store` (provenance) and `build_outputs_store`
(report, normal db).

Draft contracts. `BuildSession`, `StepResult` and the session factory are the build lane's draft of the foundation
contracts (typed BuildSession / UnitOfWork / StepResult). When the foundation module lands, these names are
replaced by imports from it; nothing else in the lane changes shape.
"""
from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence, Tuple

from build_repositories_port import UnitOfWork


@dataclass(frozen=True)
class StepResult:
    """What running one build step reports: which step, and how it ran. `via` is `registered` for a step a
    subsystem registered with the registry, `file` for a step still run by loading its script (the path every
    subsystem starts on, and leaves when its lane switches)."""
    step: str
    via: str


class BuildSession(Protocol):
    """Pipeline session for one build database.

    `con` remains the compatibility handle for unmigrated batch steps and provenance adapters. Application-level
    commits go through `unit_of_work`, which is the only commit surface exposed by the build service.
    """
    con: Any
    context: Any
    unit_of_work: UnitOfWork

    def apply_schema(self, sources: Sequence[str]) -> None: ...
    def optimize(self) -> None: ...
    def close(self) -> None: ...


class SessionFactory(Protocol):
    """Opens a session on a fresh database file; owned by the composition root, which knows the step context."""
    def open(self, path: str) -> BuildSession: ...


class StepRegistry(Protocol):
    """The ordered steps and how each one runs. `order` is which steps a build with these client sources runs,
    in order; `run` executes one of them in `session`."""
    def order(self, sources: Sequence[str]) -> Sequence[str]: ...
    def run(self, step: str, session: BuildSession) -> StepResult: ...


@dataclass(frozen=True)
class RepoState:
    commit: Optional[str]
    dirty: Optional[bool]


@dataclass(frozen=True)
class InputState:
    """One input by its stable key (never a path): `state` is `present` or `missing`; the rest are None when missing."""
    key: str
    state: str
    sha256: Optional[str]
    size_bytes: Optional[int]
    file_mtime: Optional[str]


@dataclass(frozen=True)
class Provenance:
    """What produced a database: the schema version, the code and private-data commits, the interpreter and
    sqlite versions, and every input's fingerprint (sorted by key)."""
    schema_version: int
    code: RepoState
    private: RepoState
    python_version: str
    sqlite_version: str
    inputs: Tuple[InputState, ...]
    platform: Optional[str] = None   # the OS that wrote the db (sys.platform): stored paths are canonical, this says where from


class ProvenanceReader(Protocol):
    """Reads the provenance of a freshly built `con` and writes it into that database (build_info/build_inputs).
    `write` does not commit; it returns the build_info id."""
    def read(self, con: Any) -> Provenance: ...
    def write(self, con: Any, provenance: Provenance, built_at: str, read_at: str) -> int: ...


@dataclass(frozen=True)
class NormalBuild:
    path: str
    summary: str


class BuildOutputs(Protocol):
    """What follows a build. `report` is the table counts and size of a built database, as lines. `build_normal`
    builds and leak-checks knowledge-normal.db in `directory` from the full database at `full_path` (a bad
    privacy-rules file raises: fail closed). `discard_normal` removes the
    knowledge-normal.db in `directory` and its sidecars, whether or not one exists."""
    def report(self, path: str) -> Sequence[str]: ...
    def build_normal(self, full_path: str, directory: str) -> NormalBuild: ...
    def discard_normal(self, directory: str) -> None: ...
