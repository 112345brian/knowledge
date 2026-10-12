"""What the build knows about optional client sources, as data the client hands it: domain, no file, no db.

The base owns the core pipeline and the core schema. A client source is described by a `ClientSource` and registered by
the client's own `client/bootstrap.py`
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
    path: str                       # relative to the repo root, e.g. `client/my_step.py`
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
    # Insert a report table after another core or client table. Unpositioned tables follow all core tables.
    report_after: Optional[Mapping[str, str]] = None
    data_files: Tuple[str, ...] = ()
    leak_rows: Tuple[Tuple[str, tuple], ...] = ()   # (sql, params) the leak self-test seeds into a full db
    tools: Tuple[Tool, ...] = ()
    personal_fact_keys: Tuple[str, ...] = ()  # fact-file metadata whose presence makes the heuristic flag personal


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
        own_steps: Dict[str, Step] = {}
        for step in source.steps:
            if step.path in own_steps and own_steps[step.path] != step:
                raise ValueError(f"source {source.name!r} declares step {step.path!r} differently")
            own_steps[step.path] = step
        own_inputs: Dict[str, Input] = {}
        for item in source.inputs:
            if item.key in own_inputs and own_inputs[item.key] != item:
                raise ValueError(f"source {source.name!r} declares input key {item.key!r} differently")
            own_inputs[item.key] = item
        own_tools: Dict[str, Tool] = {}
        for tool in source.tools:
            if tool.command in own_tools and own_tools[tool.command] != tool:
                raise ValueError(f"source {source.name!r} declares tool command {tool.command!r} differently")
            own_tools[tool.command] = tool
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
        if source.report_after and set(source.report_after) - set(source.report_tables):
            raise ValueError(f"source {source.name!r} positions tables it does not report: "
                             f"{sorted(set(source.report_after) - set(source.report_tables))}")
        for other in self._sources.values():
            shared = set(source.report_tables) & set(other.report_tables)
            for table in shared:
                here = (source.report_after or {}).get(table)
                there = (other.report_after or {}).get(table)
                if here != there:
                    raise ValueError(f"sources {other.name!r} and {source.name!r} position report table "
                                     f"{table!r} differently")
            input_by_key = {item.key: item for item in other.inputs}
            conflicts = [item.key for item in source.inputs
                         if item.key in input_by_key and item != input_by_key[item.key]]
            if conflicts:
                raise ValueError(f"sources {other.name!r} and {source.name!r} declare input keys "
                                 f"{conflicts} differently")
            tool_by_command = {tool.command: tool for tool in other.tools}
            conflicts = [tool.command for tool in source.tools
                         if tool.command in tool_by_command and tool != tool_by_command[tool.command]]
            if conflicts:
                raise ValueError(f"sources {other.name!r} and {source.name!r} declare tool commands "
                                 f"{conflicts} differently")
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
        active_paths = set(self._core) | set(steps)
        missing_anchors = sorted({s.after for s in steps.values() if s.after is not None and s.after not in active_paths})
        if missing_anchors:
            raise ValueError(f"enabled client step follows a disabled or unknown step: {missing_anchors}")
        by_anchor: Dict[Optional[str], List[Step]] = {}
        for s in steps.values():
            by_anchor.setdefault(s.after, []).append(s)

        visiting = set()
        visited = set()

        def with_followers(path):
            if path in visiting:
                raise ValueError(f"client pipeline step cycle at {path!r}")
            if path in visited:
                return []
            visiting.add(path)
            out = [path]
            for s in sorted(by_anchor.get(path, ()), key=lambda s: (s.order, s.path)):
                out += with_followers(s.path)
            visiting.remove(path)
            visited.add(path)
            return out

        out: List[str] = []
        for core in self._core:
            out += with_followers(core)
        for s in sorted(by_anchor.get(None, ()), key=lambda s: (s.order, s.path)):
            out += with_followers(s.path)
        if len(visited) != len(self._core) + len(steps):
            raise ValueError("client pipeline contains steps that are not reachable from the core pipeline")
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
        """Core tables and positioned client tables in declared report order, filtered to the built database."""
        after: Dict[str, List[str]] = {}
        wanted = list(core_tables)
        report_names = set(wanted)
        placement: Dict[str, Optional[str]] = {}
        for src in self._sources.values():
            positions = src.report_after or {}
            for table in src.report_tables:
                report_names.add(table)
                anchor = positions.get(table)
                if table in placement and placement[table] != anchor:
                    raise ValueError(f"client sources position report table {table!r} differently")
                placement[table] = anchor
                if anchor is None:
                    wanted.append(table)
                else:
                    after.setdefault(anchor, []).append(table)

        unknown_anchors = sorted(anchor for anchor in after if anchor not in report_names)
        if unknown_anchors:
            raise ValueError(f"client report table follows an unknown table: {unknown_anchors}")

        ordered: List[str] = []
        visiting = set()
        visited = set()

        def add(table):
            if table in visiting:
                raise ValueError(f"client report table order cycle at {table!r}")
            if table in visited:
                return
            visiting.add(table)
            ordered.append(table)
            for follower in after.get(table, ()):
                add(follower)
            visiting.remove(table)
            visited.add(table)

        for table in wanted:
            add(table)
        if len(visited) != len(report_names):
            raise ValueError("client report table order contains unreachable tables")
        existing = set(existing)
        return [t for t in ordered if t in existing]

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

    def personal_fact_keys(self) -> Tuple[str, ...]:
        """Client-owned fact metadata keys that contribute to the core personal-topic heuristic."""
        return tuple(dict.fromkeys(key for src in self._sources.values() for key in src.personal_fact_keys))

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
    def personal_fact_keys(self) -> Tuple[str, ...]: ...
