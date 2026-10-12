"""The foundation's contracts: the ports are SQL-free and typed, the fakes satisfy them, a session and a bound
pipeline behave as documented, and the results the pipeline returns cannot be silently dropped.

`tests/fake_build_repositories.py` is the in-memory world a lane builds its step tests on; this file is the
template a lane copies: a step is a frozen `*Deps` plus a `run() -> StepResult`.
"""
import ast
import dataclasses
import inspect
import os
import typing
from dataclasses import dataclass
from typing import Tuple

import pytest

import build_composition as comp
import build_records as r
import build_repositories_port as ports
import build_steps_port as steps
import wiring
from fake_build_repositories import (CollectingDiagnostics, FakeInputs, FakeSessionOpener, FakeState,
                                     fake_session)
from locations import Locations

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPOSITORIES = {n: p for n, p in ports.REPOSITORY_PORTS.items() if n != "unit_of_work"}

SQL_WORDS = {"execute", "executemany", "executescript", "cursor", "sql", "query", "queries", "statement_sql", "con",
             "conn", "connection", "cur", "row", "rows", "raw"}


def _methods(proto):
    return wiring._protocol_members(proto)[0]


# ------------------------------------------------------------------------------------------- port shape

@pytest.mark.parametrize("name", sorted(ports.REPOSITORY_PORTS))
def test_a_port_exposes_no_sql_execution_surface(name):
    proto = ports.REPOSITORY_PORTS[name]
    for method, fn in _methods(proto).items():
        assert method.lower() not in SQL_WORDS and "sql" not in method.lower() and "execute" not in method.lower(), \
            f"{name}.{method} looks like a SQL surface"
        for pname in inspect.signature(fn).parameters:
            assert pname not in SQL_WORDS, f"{name}.{method}({pname}): a port takes records and identifiers"
        text = str(typing.get_type_hints(fn))
        assert "sqlite3" not in text and "Cursor" not in text and "Connection" not in text, f"{name}.{method}: {text}"


def test_the_port_modules_import_no_database_or_file_module():
    banned = {"sqlite3", "os", "pathlib", "subprocess", "shutil", "tempfile", "glob", "io", "json"}
    for module in ("build_repositories_port", "build_steps_port", "build_records", "identity_rules"):
        tree = ast.parse(open(os.path.join(REPO, module + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                [(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) else []
            assert not banned & set(names), f"{module}.py:{node.lineno} imports {sorted(banned & set(names))}"


def test_every_repository_method_is_annotated():
    for name, proto in REPOSITORIES.items():
        for method, fn in _methods(proto).items():
            hints = typing.get_type_hints(fn)
            params = [p for p in inspect.signature(fn).parameters if p != "self"]
            assert "return" in hints and all(p in hints for p in params), f"{name}.{method} is not fully annotated"


def test_every_record_is_a_frozen_dataclass():
    records = [c for _, c in inspect.getmembers(r, inspect.isclass) if c.__module__ == "build_records"]
    assert len(records) >= 12
    for cls in records:
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, cls.__name__  # type: ignore[attr-defined]


def test_the_session_holds_exactly_the_ports_and_is_frozen():
    fields = {f.name for f in dataclasses.fields(ports.BuildSession)}
    assert fields == set(ports.REPOSITORY_PORTS)
    session = fake_session()
    with pytest.raises(dataclasses.FrozenInstanceError):
        session.facts = None    # type: ignore[misc]


def test_pipeline_session_exposes_commit_only_through_unit_of_work():
    import build_port
    assert "commit" not in vars(build_port.BuildSession)
    assert {name for name, fn in vars(ports.UnitOfWork).items() if callable(fn) and not name.startswith("_")} == {"commit"}
    assert "unit_of_work" in build_port.BuildSession.__annotations__


# --------------------------------------------------------------------------------------------- the fakes

@pytest.mark.parametrize("name", sorted(ports.REPOSITORY_PORTS))
def test_the_fakes_satisfy_their_ports_by_name_and_signature(name, monkeypatch):
    """Same check wiring.verify applies to production adapters: every protocol member exists and accepts the calls
    the protocol allows (minimal, maximal, by name)."""
    monkeypatch.setitem(wiring.PORT_PROTOCOLS, f"build.{name}", ports.REPOSITORY_PORTS[name])
    assert wiring.conformance_problems(f"build.{name}", getattr(fake_session(), name)) == []


def test_conformance_check_fails_for_a_fake_missing_a_method(monkeypatch):
    monkeypatch.setitem(wiring.PORT_PROTOCOLS, "build.facts", ports.FactRepository)

    class Missing:
        def add(self, fact):
            raise NotImplementedError
    assert any("tally is missing" in p for p in wiring.conformance_problems("build.facts", Missing()))


def test_conformance_check_fails_for_a_fake_missing_an_inherited_method(monkeypatch):
    from typing import Protocol

    class Base(Protocol):
        def inherited(self) -> None: ...

    class Derived(Base, Protocol):
        def own(self) -> None: ...

    class MissingInherited:
        def own(self) -> None:
            pass

    monkeypatch.setitem(wiring.PORT_PROTOCOLS, "build.inherited_test", Derived)
    problems = wiring.conformance_problems("build.inherited_test", MissingInherited())
    assert any("inherited is missing" in problem for problem in problems)


def test_fake_ids_are_allocated_per_table_in_call_order():
    state = FakeState()
    s = fake_session(state)
    ids = [s.sources.add(r.NewSource(f"k{i}", "n", "primary")) for i in range(3)]
    sub = [s.subjects.add(f"s{i}") for i in range(2)]
    assert ids == [1, 2, 3] and sub == [1, 2]
    assert [op[0] for op in state.operations] == ["sources.add"] * 3 + ["subjects.add"] * 2


def test_fake_unit_of_work_counts_commits_in_the_operation_log():
    state = FakeState()
    s = fake_session(state)
    s.subjects.add("a")
    s.unit_of_work.commit()
    s.unit_of_work.commit()
    assert state.commits == 2 and state.operations[-2:] == [("commit",), ("commit",)]


# ------------------------------------------------------------------------------ step result and the runner

def test_step_result_needs_exactly_one_bracketed_headline():
    ok = steps.StepResult("s", ("  WARNING", "[s] did it", "  detail"), {"n": 1})
    assert ok.headline == "[s] did it"
    for bad in ((), ("[s] a", "[s] b"), ("[other] a",)):
        with pytest.raises(ValueError):
            steps.StepResult("s", bad, {})


@dataclass(frozen=True)
class ExampleDeps:
    """A step's own dependencies: only the ports it uses, plus the values it reads. (Template for a lane.)"""
    sources: ports.SourceRepository
    unit_of_work: ports.UnitOfWork
    diagnostics: steps.Diagnostics
    locations: Locations


class ExampleStep:
    name = "example"

    def __init__(self, deps: ExampleDeps, keys: Tuple[str, ...]):
        self._d = deps
        self._keys = keys

    def run(self) -> steps.StepResult:
        added = skipped = 0
        for key in self._keys:
            if self._d.sources.find(key) is not None:
                self._d.diagnostics.emit(f"  WARNING -- {key} exists")
                skipped += 1
                continue
            self._d.sources.add(r.NewSource(key, key, "primary"))
            added += 1
        self._d.unit_of_work.commit()
        return steps.StepResult("example", (f"[example] added {added}, skipped {skipped}",), {"added": added, "skipped": skipped})


def _locations():
    return Locations(knowledge_db_dir="/db", private_data_dir="/data")


def _registry(keys=("a", "b", "a")):
    def factory(session, env):
        deps = ExampleDeps(session.sources, session.unit_of_work, env.diagnostics, env.locations)
        return ExampleStep(deps, keys)
    return {"ingest/example.py": factory}


def test_a_bound_step_runs_against_fakes_and_reports_through_its_result():
    state = FakeState()
    diag = CollectingDiagnostics()
    env = steps.StepEnvironment(_locations(), FakeInputs(), diag)
    binder = comp.PipelineBinder(_registry())
    bound = binder.bind(fake_session(state), ["ingest/example.py"], env)
    out = []
    results = comp.run_pipeline(bound, out.append)
    assert results[0].counts == {"added": 2, "skipped": 1}      # "a" repeated: the second is found, warned, skipped
    assert out == ["[example] added 2, skipped 1"]
    assert diag.lines == ["  WARNING -- a exists"]
    assert state.commits == 1


def test_warnings_are_emitted_while_the_step_runs_and_lines_after_it_returns():
    diag = CollectingDiagnostics()
    env = steps.StepEnvironment(_locations(), FakeInputs(), diag)
    seen = []

    class Twice:
        name = "t"

        def run(self):
            diag.emit("  WARNING -- mid-run")
            seen.append(list(diag.lines))
            return steps.StepResult("t", ("[t] done",), {})
    printed = []

    def emit(line):
        printed.append(line)
    comp.run_pipeline([steps.BoundStep("ingest/t.py", Twice())], emit)
    assert seen == [["  WARNING -- mid-run"]] and printed == ["[t] done"]
    assert env.diagnostics is diag


def test_the_binder_follows_the_given_order_and_refuses_an_unbound_step():
    env = steps.StepEnvironment(_locations(), FakeInputs(), CollectingDiagnostics())
    binder = comp.PipelineBinder({**_registry(), "ingest/other.py": _registry(("z",))["ingest/example.py"]})
    session = fake_session()
    assert [b.path for b in binder.bind(session, ["ingest/other.py", "ingest/example.py"], env)] == \
        ["ingest/other.py", "ingest/example.py"]
    assert binder.unbound(["ingest/example.py", "client/missing.py"]) == ["client/missing.py"]
    with pytest.raises(comp.UnboundSteps) as e:
        binder.bind(session, ["ingest/example.py", "client/missing.py"], env)
    assert e.value.paths == ("client/missing.py",)


def test_a_failing_step_is_wrapped_with_its_stage_and_stops_the_pipeline():
    class Boom:
        name = "boom"

        def run(self):
            raise ValueError("bad input")

    class Never:
        name = "never"
        ran = False

        def run(self):
            Never.ran = True
            return steps.StepResult("never", ("[never] x",), {})
    printed = []
    with pytest.raises(steps.StepError) as e:
        comp.run_pipeline([steps.BoundStep("ingest/boom.py", Boom()), steps.BoundStep("ingest/never.py", Never())],
                          printed.append)
    assert (e.value.stage, str(e.value), type(e.value.cause)) == ("ingest/boom.py", "ingest/boom.py: bad input", ValueError)
    assert printed == [] and Never.ran is False


def test_a_keyboard_interrupt_is_not_a_step_failure():
    class Stop:
        name = "stop"

        def run(self):
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        comp.run_pipeline([steps.BoundStep("ingest/stop.py", Stop())], lambda line: None)


def test_session_factory_opens_closes_and_closes_on_error():
    opener = FakeSessionOpener()
    factory = comp.BuildSessionFactory(opener)
    with factory.open("/tmp/build.db") as session:
        session.unit_of_work.commit()
    with pytest.raises(RuntimeError):
        with factory.open("/tmp/second.db"):
            raise RuntimeError("step failed")
    assert opener.opened == ["/tmp/build.db", "/tmp/second.db"] and opener.closed == 2
    assert opener.state.commits == 1


def test_error_message_shape_matches_the_build_driver_error():
    import build
    legacy = build.BuildError("ingest/facts.py", ValueError("x"))
    new = steps.StepError("ingest/facts.py", ValueError("x"))
    assert (str(legacy), legacy.stage, type(legacy.cause)) == (str(new), new.stage, type(new.cause))


# -------------------------------------------------- results are never silently dropped (caller scan)

def test_no_caller_discards_a_pipeline_result_or_a_step_result():
    """`run_pipeline` returns the StepResults and `StepResult.lines` are what the runner prints; a caller that
    calls it as a bare statement, or a bound step's `.run()` outside the runner, would lose the output."""
    offenders = []
    for folder in ("", "ingest", "client"):
        base = os.path.join(REPO, folder)
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py") or name in ("build_composition.py",):
                continue
            tree = ast.parse(open(os.path.join(base, name), encoding="utf-8").read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    fn = ast.unparse(node.value.func)
                    if fn.split(".")[-1] == "run_pipeline":
                        offenders.append(f"{name}:{node.lineno}: result of {fn}() is discarded")
    assert offenders == []


def test_the_composition_runner_uses_every_result_it_gets():
    tree = ast.parse(open(os.path.join(REPO, "build_composition.py"), encoding="utf-8").read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_pipeline")
    discarded = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                 and ast.unparse(n.value.func).endswith(".run")]
    assert discarded == []
