"""The rebuild use case against in-memory fakes: no file, no database, no clock. Order, failure naming,
publication and the supported optional client-source combinations; the disk-level recovery tests are in
tests/test_build.py."""
import itertools
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import build_rules  # noqa: E402
import build_service  # noqa: E402
import building  # noqa: E402
from build_port import NormalBuild, StepResult  # noqa: E402
from build_rules import BuildError, NormalBuildError  # noqa: E402
from locations import Locations  # noqa: E402


class FakeFiles:
    """A dict of path -> bytes standing in for the disk."""
    def __init__(self, existing=None):
        self.fs = dict(existing or {})
        self.tokens = itertools.count(1)
        self.log = []

    def join(self, directory, name):
        return f"{directory}/{name}"

    def exists(self, path):
        return path in self.fs

    def create_exclusive(self, path):
        if path in self.fs:
            raise FileExistsError(path)
        self.fs[path] = b""

    def remove_if_exists(self, path):
        self.fs.pop(path, None)

    def makedirs(self, path):
        pass

    def replace(self, source, target):
        self.log.append(("replace", source, target))
        self.fs[target] = self.fs.pop(source)

    def copy(self, source, target):
        self.fs[target] = self.fs[source]

    def listdir(self, path):
        return [p[len(path) + 1:] for p in self.fs if p.startswith(path + "/") and "/" not in p[len(path) + 1:]]

    def remove(self, path):
        del self.fs[path]

    def unique_token(self):
        return f"t{next(self.tokens)}"

    def system_temp_dir(self):
        return "/tmp"

    def temporary_directory(self):
        import contextlib
        return contextlib.nullcontext("/tmp/scratch")


class FakeSession:
    def __init__(self, files, path, calls, fail=None):
        self.files, self.path, self.calls, self.fail = files, path, calls, fail
        self.con = object()
        self.context = None

    def apply_schema(self, sources):
        self.calls.append(("schema", tuple(sources)))
        if self.fail == "schema":
            raise RuntimeError("bad schema")
        self.files.fs[self.path] = b"built"

    def commit(self):
        self.calls.append(("commit",))

    def optimize(self):
        self.calls.append(("optimize",))
        if self.fail == "finalize":
            raise RuntimeError("vacuum failed")

    def close(self):
        self.calls.append(("close",))


class FakeSessions:
    def __init__(self, files, calls, fail=None):
        self.files, self.calls, self.fail = files, calls, fail

    def open(self, path):
        if self.fail == "open":
            raise OSError("cannot open")
        return FakeSession(self.files, path, self.calls, self.fail)


class FakeSteps:
    def __init__(self, calls, fail_step=None):
        self.calls, self.fail_step = calls, fail_step

    def order(self, sources):
        return build_rules.steps_for(sources)

    def run(self, step, session):
        self.calls.append(("step", step))
        if step == self.fail_step:
            raise RuntimeError("boom")
        return StepResult(step, "file")


class FakeProvenance:
    def __init__(self, calls, fail=False):
        self.calls, self.fail = calls, fail

    def read(self, con):
        self.calls.append(("provenance.read",))
        if self.fail:
            raise RuntimeError("git exploded")
        return "facts"

    def write(self, con, provenance, built_at, read_at):
        self.calls.append(("provenance.write", provenance, built_at))
        return 1


class FakeClock:
    is_fixed = True

    def now(self):
        import datetime
        return datetime.datetime(2026, 10, 3, 8, 0, 0)

    def now_iso(self):
        return "2026-10-03T08:00:00+00:00"


class FakeOutputs:
    def __init__(self, calls, fail=False):
        self.calls, self.fail = calls, fail

    def report(self, path):
        self.calls.append(("report", path))
        return ["  facts: 0"]

    def build_normal(self, full_path, directory):
        self.calls.append(("normal", full_path, directory))
        if self.fail:
            raise RuntimeError("leak")
        return NormalBuild(f"{directory}/knowledge-normal.db", "counts")

    def discard_normal(self, directory):
        self.calls.append(("discard_normal", directory))


def make(files=None, sources=(), fail=None, fail_step=None, provenance_fails=False, outputs_fail=False, **loc):
    calls = []
    files = files or FakeFiles()
    locations = Locations(knowledge_db_dir="/db", private_data_dir="/data", client_sources=tuple(sources), **loc)
    deps = build_service.BuildDeps(files=files, clock=FakeClock(), locations=locations,
                                   sessions=FakeSessions(files, calls, fail), steps=FakeSteps(calls, fail_step),
                                   provenance=FakeProvenance(calls, provenance_fails), outputs=FakeOutputs(calls, outputs_fail))
    return deps, files, calls


def test_a_build_runs_schema_steps_provenance_then_finalizes_in_that_order():
    deps, files, calls = make()
    results = build_service.build_database(deps, "/db/x")
    steps = build_rules.steps_for(())
    assert calls == [("schema", ())] + [("step", s) for s in steps] + [
        ("provenance.read",), ("provenance.write", "facts", "2026-10-03T08:00:00+00:00"), ("commit",), ("optimize",), ("close",)]
    assert [r.step for r in results] == steps


@pytest.mark.parametrize("fail, step, stage", [
    ("open", None, "open"), ("schema", None, "schema"), (None, "ingest/facts.py", "ingest/facts.py"),
    ("finalize", None, "finalize")])
def test_every_failing_stage_is_named_and_the_session_is_closed(fail, step, stage):
    deps, files, calls = make(fail=fail, fail_step=step)
    with pytest.raises(BuildError) as ei:
        build_service.build_database(deps, "/db/x")
    assert ei.value.stage == stage
    assert ("close",) in calls or fail == "open"
    assert ("commit",) not in calls or fail == "finalize"


def test_a_provenance_failure_is_a_build_info_failure_before_any_commit():
    deps, files, calls = make(provenance_fails=True)
    with pytest.raises(BuildError) as ei:
        build_service.build_database(deps, "/db/x")
    assert ei.value.stage == "build_info" and ("commit",) not in calls and ("optimize",) not in calls


def test_a_failing_step_stops_the_steps_after_it():
    deps, files, calls = make(fail_step="ingest/facts.py")
    with pytest.raises(BuildError):
        build_service.build_database(deps, "/db/x")
    ran = [c[1] for c in calls if c[0] == "step"]
    assert ran == build_rules.steps_for(())[:build_rules.steps_for(()).index("ingest/facts.py") + 1]


def test_a_failure_before_publication_leaves_the_live_database_and_the_temp_file_alone():
    files = FakeFiles({"/db/knowledge.db": b"previous full db"})
    deps, files, calls = make(files, fail_step="ingest/facts.py")
    with pytest.raises(BuildError):
        build_service.rebuild(deps)
    assert files.fs == {"/db/knowledge.db": b"previous full db"}       # no temp file, no backup, previous bytes
    assert not any(c[0] in ("report", "normal") for c in calls)


def test_rebuild_publishes_then_builds_the_normal_db_from_the_published_file():
    files = FakeFiles({"/db/knowledge.db": b"previous"})
    deps, files, calls = make(files)
    lines = []
    outcome = build_service.rebuild(deps, lines.append)
    assert files.fs["/db/knowledge.db"] == b"built"
    assert outcome.backup_path == "/db/backups/knowledge.db.bak-20261003T080000"
    assert files.fs[outcome.backup_path] == b"previous"
    assert ("normal", "/db/knowledge.db", "/db") in calls
    assert calls.index(("report", "/db/knowledge.db")) < calls.index(("normal", "/db/knowledge.db", "/db"))
    assert not [p for p in files.fs if ".building-" in p or "partial" in p]


def test_normal_failure_after_publication_keeps_the_new_db_and_discards_the_normal_artifact():
    deps, files, calls = make(FakeFiles({"/db/knowledge.db": b"previous"}), outputs_fail=True)
    with pytest.raises(NormalBuildError) as ei:
        build_service.rebuild(deps)
    assert ei.value.published and isinstance(ei.value, BuildError)
    assert files.fs["/db/knowledge.db"] == b"built"
    assert ("discard_normal", "/db") in calls


def test_check_builds_nothing_that_exists_and_cleans_its_temp_file():
    files = FakeFiles({"/db/knowledge.db": b"previous"})
    deps, files, calls = make(files)
    outcome = build_service.check(deps)
    assert files.fs == {"/db/knowledge.db": b"previous"} and outcome.live_path is None
    assert ("normal", "/tmp/knowledge.db.building-t1", "/tmp/scratch") in calls
    assert not any(c[0] == "discard_normal" for c in calls)


def test_check_normal_failure_is_not_published_and_discards_nothing():
    deps, files, calls = make(outputs_fail=True)
    with pytest.raises(NormalBuildError) as ei:
        build_service.check(deps)
    assert not ei.value.published
    assert files.fs == {} and not any(c[0] == "discard_normal" for c in calls)


# ---- the supported optional client-source combinations ----------------------------------------------------

ALL_COMBINATIONS = [c for r in range(len(build_rules.CLIENT_SOURCES) + 1)
                    for c in itertools.combinations(build_rules.CLIENT_SOURCES, r)]
INPUTS = dict(concerts_csv="/in/c.csv", rym_export_csv="/in/r.csv", scrobbles_json="/in/s.json")


@pytest.mark.parametrize("sources", ALL_COMBINATIONS, ids=lambda c: "+".join(c) or "core")
def test_every_client_source_combination_builds_with_its_inputs_and_schema_fragments(sources):
    deps, files, calls = make(sources=sources, **INPUTS)
    build_service.build_database(deps, "/db/x")
    schema_call = next(c for c in calls if c[0] == "schema")
    assert schema_call == ("schema", sources)
    ran = [c[1] for c in calls if c[0] == "step"]
    assert ran == build_rules.steps_for(sources)
    # the core steps always run, in the pipeline's order; a client step runs only when its source is enabled
    core = [p for p, source in build_rules.PIPELINE if source is None]
    assert [s for s in ran if s in core] == core
    for path, source in build_rules.PIPELINE:
        wanted = source is None or any(n in sources for n in ((source,) if isinstance(source, str) else source))
        assert (path in ran) == wanted


@pytest.mark.parametrize("source, missing", [("concerts", "CONCERTS_CSV"), ("ratings", "RYM_EXPORT_CSV"),
                                             ("scrobbles", "SCROBBLES_JSON")])
def test_an_enabled_source_without_its_input_is_a_config_failure_that_runs_nothing(source, missing):
    deps, files, calls = make(sources=(source,))
    with pytest.raises(BuildError) as ei:
        build_service.build_database(deps, "/db/x")
    assert ei.value.stage == "config" and missing in str(ei.value) and calls == []


def test_sources_that_need_no_input_file_build_without_one():
    deps, files, calls = make(sources=("measurements", "claims"))
    build_service.build_database(deps, "/db/x")


def test_an_unknown_or_repeated_source_is_refused_before_anything_runs():
    for sources in (("concerts", "nonsense"), ("claims", "claims")):
        deps, files, calls = make(sources=sources, **INPUTS)
        with pytest.raises(ValueError):
            build_service.build_database(deps, "/db/x")
        assert calls == []


# ---- the composed step registry -----------------------------------------------------------------------------

class _Session:
    con, context = "con", "ctx"


class _Loader:
    def __init__(self):
        self.loaded = []

    def join(self, d, n):
        return f"{d}/{n}"

    def load_module(self, path):
        self.loaded.append(path)

        class M:
            @staticmethod
            def run(con, ctx):
                self.loaded.append((con, ctx))
        return M


def test_an_unregistered_step_runs_by_loading_its_script_with_the_legacy_signature():
    loader = _Loader()
    result = building.Registry(loader, "/code").run("ingest/facts.py", _Session())
    assert result == StepResult("ingest/facts.py", "file")
    assert loader.loaded == ["/code/ingest/facts.py", ("con", "ctx")]


def test_a_registered_handler_replaces_the_script_and_never_loads_it():
    loader, seen = _Loader(), []
    registry = building.Registry(loader, "/code")
    registry.register("ingest/facts.py", seen.append)
    session = _Session()
    assert registry.run("ingest/facts.py", session) == StepResult("ingest/facts.py", "registered")
    assert seen == [session] and loader.loaded == []
    assert registry.registered() == ("ingest/facts.py",)


def test_the_registry_refuses_an_unknown_step_and_a_second_handler():
    registry = building.Registry(_Loader(), "/code")
    with pytest.raises(ValueError, match="not a build step"):
        registry.register("ingest/typo.py", print)
    registry.register("ingest/facts.py", print)
    with pytest.raises(ValueError, match="already has"):
        registry.register("ingest/facts.py", print)


def test_the_registry_orders_steps_like_the_pipeline_unless_given_a_fixed_list():
    assert building.Registry(_Loader(), "/c").order(("claims",)) == build_rules.steps_for(("claims",))
    assert building.Registry(_Loader(), "/c", only=["x.py"]).order(("claims",)) == ["x.py"]


def test_every_pipeline_step_is_still_run_the_legacy_way_until_a_lane_registers_it():
    """No subsystem has switched yet: the shipped registry has no handlers, so every step keeps its execution path."""
    import local_buildfiles
    assert building.compose_registry(local_buildfiles).registered() == ()


# ---- backup names ---------------------------------------------------------------------------------------------

def test_backup_order_is_numeric_so_ten_is_newer_than_nine():
    base = "knowledge.db.bak-20261003T080000"
    names = [build_rules.backup_candidate(base, n) for n in range(1, 13)]
    assert set(build_rules.backups_to_prune(names, keep=5)) == {base} | {f"{base}-{n}" for n in range(2, 8)}
    older = "knowledge.db.bak-20261002T235959"
    assert build_rules.backups_to_prune([older, base, f"{base}-2", "notes.txt"], keep=2) == [older]


def test_the_next_backup_number_is_past_the_highest_even_after_the_oldest_was_pruned():
    base = "knowledge.db.bak-20261003T080000"
    assert build_rules.next_backup_attempt(base, []) == 1
    assert build_rules.next_backup_attempt(base, [base]) == 2
    assert build_rules.next_backup_attempt(base, [f"{base}-6", f"{base}-9", "knowledge.db.bak-20261003T080001-30"]) == 10


# ---- composition: the container owns the registry and the session factory, and wiring verifies the ports ----

def test_the_container_owns_one_registry_and_one_session_factory_for_the_rebuild():
    import container
    app = container.Container(Locations(knowledge_db_dir="/db", private_data_dir="/data"))
    assert app.build_steps is app.build_steps and app.build_sessions is app.build_sessions
    use_case = app.build_use_case(FakeOutputs([]))
    assert use_case.deps.steps is app.build_steps and use_case.deps.sessions is app.build_sessions


def test_composition_rejects_an_adapter_that_does_not_have_the_shape_of_its_port():
    import container
    import wiring

    class NoOpen:
        pass
    app = container.Container(Locations(knowledge_db_dir="/db", private_data_dir="/data"))
    with pytest.raises(wiring.PortMismatch, match="sessions: open is missing"):
        building.compose(app.locations, app.clock, FakeOutputs([]), sessions=NoOpen())

    class RenamedReport:
        def build_normal(self, full_path, directory): ...
        def discard_normal(self, directory): ...
    with pytest.raises(wiring.PortMismatch, match="outputs: report is missing"):
        building.compose(app.locations, app.clock, RenamedReport())
