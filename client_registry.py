"""What the build knows about optional client sources, as data the client hands it: domain, no file, no db.

The base owns the core pipeline and the core schema. A client source (music, measurements, anything a checkout adds
under `client/`) is described by a `ClientSource` and registered by the client's own `client/bootstrap.py`
(`client_bootstrap` calls its `register(registry)` once the client directory is recognised). Nothing in the base
names a client source, a client step, a client table or a client input: it asks this registry.

A source says where its steps go (`Step.after` names the step it follows, a core step or one of its own; steps
that follow the same step are ordered by `order`, then path), which schema files it needs, which `local_paths.py`
settings it reads, which tables the build report counts and which private data files it owns. Declarations shared
by several sources (two sources of one client may share a schema file and a step) are listed in each and
applied once.
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class Step:
    path: str                       # relative to the repo root: `client/concerts.py`
    after: Optional[str] = None     # the step this one follows (core or client); None = at the end of the pipeline
    order: int = 0                  # among steps that follow the same step


@dataclass(frozen=True)
class Input:
    """A file the source reads, for provenance and (when `required`) for the configuration check. `setting` is the
    `local_paths.py` name: a client setting (`MY_FEED_JSON`) or a core directory (`NOTES_VAULT`); `file` is joined
    onto a directory setting."""
    setting: str
    key: str                        # the stable provenance key, without the `input:` prefix
    file: Optional[str] = None
    required: bool = False


@dataclass(frozen=True)
class Tool:
    """A standalone command a source ships (`knowledge.py <command>`): `module` is run as `python -m <module>`."""
    command: str
    module: str
    help: str = ""


@dataclass(frozen=True)
class ClientSource:
    name: str
    steps: Tuple[Step, ...] = ()
    schema: Tuple[str, ...] = ()
    inputs: Tuple[Input, ...] = ()
    report_tables: Tuple[str, ...] = ()
    data_files: Tuple[str, ...] = ()
    leak_rows: Tuple[Tuple[str, tuple], ...] = ()   # (sql, params) the leak self-test seeds into a full db
    tools: Tuple[Tool, ...] = ()


@dataclass(frozen=True)
class _Implicit:
    trigger: str                    # a setting; when it is defined and no CLIENT_SOURCES is declared ...
    sources: Tuple[str, ...]        # ... these are enabled (checkouts written before CLIENT_SOURCES existed)


class ClientRegistry:
    def __init__(self, core_pipeline: Sequence[str]):
        self._core = tuple(core_pipeline)
        self._sources: Dict[str, ClientSource] = {}
        self._implicit: List[_Implicit] = []

    # ---- registration

    def add(self, source: ClientSource) -> None:
        if source.name in self._sources:
            raise ValueError(f"client source {source.name!r} is registered twice")
        known = set(self._core) | {s.path for src in self._sources.values() for s in src.steps}
        known |= {s.path for s in source.steps}
        for step in source.steps:
            if step.after is not None and step.after not in known:
                raise ValueError(f"client step {step.path!r} follows {step.after!r}, which is not a pipeline step")
            if step.path in self._core:
                raise ValueError(f"client step {step.path!r} is a core step")
        for other in self._sources.values():
            mine = {s.path: s for s in source.steps}
            clash = [s.path for s in other.steps if s.path in mine and mine[s.path] != s]
            if clash:
                raise ValueError(f"sources {other.name!r} and {source.name!r} declare {clash} differently")
        self._sources[source.name] = source

    def enable_when_defined(self, setting: str, sources: Iterable[str]) -> None:
        """Checkouts that predate CLIENT_SOURCES: with none declared and `setting` defined, enable `sources`."""
        self._implicit.append(_Implicit(setting, tuple(sources)))

    # ---- what is registered

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(self._sources)

    def source(self, name: str) -> ClientSource:
        return self._sources[name]

    def schema_fragments(self) -> List[str]:
        """Every schema file any registered source brings, each once, in registration order."""
        return self.schema_files(self.names)[1:]

    def check(self, names: Iterable[str]) -> Tuple[str, ...]:
        """`names` as a tuple, or ValueError naming an unknown or repeated source."""
        names = tuple(names)
        unknown = [n for n in names if n not in self._sources]
        if unknown:
            raise ValueError(f"unknown client source(s) {unknown} in CLIENT_SOURCES; known: {list(self._sources)}")
        if len(set(names)) != len(names):
            raise ValueError(f"CLIENT_SOURCES lists a source twice: {list(names)}")
        return names

    def implicit_sources(self, defined: Iterable[str]) -> Tuple[str, ...]:
        defined = set(defined)
        for rule in self._implicit:
            if rule.trigger in defined:
                return rule.sources
        return ()

    # ---- the build, for the enabled sources

    def _enabled(self, enabled) -> List[ClientSource]:
        """The enabled sources in registration order (not the order they were listed in)."""
        wanted = set(self.check(enabled))
        return [src for name, src in self._sources.items() if name in wanted]

    def steps_for(self, enabled) -> List[str]:
        """The step files to run, in order: the core pipeline with each enabled source's steps after their anchors."""
        steps: Dict[str, Step] = {}
        for src in self._enabled(enabled):
            for s in src.steps:
                steps.setdefault(s.path, s)
        by_anchor: Dict[Optional[str], List[Step]] = {}
        for s in steps.values():
            by_anchor.setdefault(s.after, []).append(s)

        def with_followers(path):
            out = [path]
            for s in sorted(by_anchor.get(path, ()), key=lambda s: (s.order, s.path)):
                out += with_followers(s.path)
            return out

        out: List[str] = []
        for core in self._core:
            out += with_followers(core)
        for s in sorted(by_anchor.get(None, ()), key=lambda s: (s.order, s.path)):
            out += with_followers(s.path)
        return out

    def all_steps(self) -> List[str]:
        return self.steps_for(self.names)

    def schema_files(self, enabled) -> List[str]:
        """schema.sql, then each enabled source's fragments in registration order, each once."""
        out = ["schema.sql"]
        for src in self._enabled(enabled):
            out += [f for f in src.schema if f not in out]
        return out

    def missing_inputs(self, enabled, defined: Iterable[str]) -> List[Tuple[str, str]]:
        """[(source, setting)] for enabled sources whose required input is not defined."""
        defined = set(defined)
        return [(src.name, i.setting) for src in self._enabled(enabled) for i in src.inputs
                if i.required and i.setting not in defined]

    def report_tables(self, existing: Iterable[str], core_tables: Sequence[str]) -> List[str]:
        """The core tables, then the registered sources' tables, that exist in the built database."""
        wanted = list(core_tables)
        for src in self._sources.values():
            wanted += [t for t in src.report_tables if t not in wanted]
        existing = set(existing)
        return [t for t in wanted if t in existing]

    def inputs(self) -> List[Input]:
        seen, out = set(), []
        for src in self._sources.values():
            for i in src.inputs:
                if (i.key, i.setting, i.file) not in seen:
                    seen.add((i.key, i.setting, i.file))
                    out.append(i)
        return out

    def data_files(self) -> List[str]:
        out: List[str] = []
        for src in self._sources.values():
            out += [f for f in src.data_files if f not in out]
        return out

    def tools(self) -> List[Tool]:
        out: List[Tool] = []
        for src in self._sources.values():
            out += [t for t in src.tools if t not in out]
        return out

    def leak_rows(self) -> List[Tuple[str, tuple]]:
        return [r for src in self._sources.values() for r in src.leak_rows]

    def note(self, sources: Sequence[str], implicit: bool = False) -> str:
        """One line saying which optional client sources a build includes (and why, if that was a guess)."""
        if not sources:
            return "[build] client sources: none (core only; list them in CLIENT_SOURCES in local_paths.py)"
        text = f"[build] client sources: {', '.join(sources)}"
        if implicit:
            text += ("  (assumed: local_paths.py defines the inputs of these sources but no CLIENT_SOURCES; add "
                     f"CLIENT_SOURCES = {tuple(sources)!r} to make this explicit)")
        return text


class ClientSources(Protocol):
    """What the build asks of the registered client sources (`ClientRegistry` implements it)."""
    @property
    def names(self) -> Tuple[str, ...]: ...
    def check(self, names: Iterable[str]) -> Tuple[str, ...]: ...
    def implicit_sources(self, defined: Iterable[str]) -> Tuple[str, ...]: ...
    def note(self, sources: Sequence[str], implicit: bool = False) -> str: ...
    def missing_inputs(self, enabled, defined: Iterable[str]) -> List[Tuple[str, str]]: ...
    def steps_for(self, enabled) -> List[str]: ...
    def all_steps(self) -> List[str]: ...
    def schema_files(self, enabled) -> List[str]: ...
    def report_tables(self, existing: Iterable[str], core_tables: Sequence[str]) -> List[str]: ...
