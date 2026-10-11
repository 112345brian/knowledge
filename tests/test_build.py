"""build.py: characterization of today's behavior, then the guarantees from
issue #17 (build to a temp file, swap atomically, a failed rebuild never loses
data). Everything runs against a temp dir with a tiny schema and fake steps, so
the real knowledge.db and the real ingest scripts are never touched.
"""
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import build  # noqa: E402
import build_rules  # noqa: E402
import build_service  # noqa: E402
import building  # noqa: E402
import local_buildfiles  # noqa: E402
from build_port import NormalBuild  # noqa: E402
from locations import Locations  # noqa: E402

SCHEMA_SQL = "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);\n"
GOOD_STEP = "def run(con, locations):\n    con.execute(\"INSERT INTO t (v) VALUES ('{v}')\")\n    con.commit()\n"
BAD_STEP = ("def run(con, locations):\n    con.execute(\"INSERT INTO t (v) VALUES ('half')\")\n    con.commit()\n"
            "    raise RuntimeError('boom')\n")


class NoProvenance:
    """The fake schema has no build_info tables; tests/test_build_info.py covers the real writer."""
    def read(self, con):
        return None

    def write(self, con, provenance, built_at, read_at):
        return 0


class NoOutputs:
    """The fake schema has none of the real tables the report counts, nor the facts the normal db is made from;
    tests/test_normal_db.py covers that hook."""
    def __init__(self):
        self.built, self.discarded = [], []

    def report(self, path):
        return []

    def build_normal(self, full_path, directory):
        self.built.append((full_path, directory))
        return NormalBuild(os.path.join(directory, "knowledge-normal.db"), "")

    def discard_normal(self, directory):
        self.discarded.append(directory)


class RecordingFiles:
    """The real disk adapter, remembering the temp build files the service created."""
    def __init__(self):
        self.build_temps = []

    def __getattr__(self, name):
        return getattr(local_buildfiles, name)

    def create_exclusive(self, path):
        if "knowledge.db.building-" in path:
            self.build_temps.append(path)
        local_buildfiles.create_exclusive(path)


@pytest.fixture
def env(tmp_path):
    """Fake pipeline in tmp_path: a fake schema and two fake steps, run through the real service and registry."""
    steps_dir = tmp_path / "steps"
    steps_dir.mkdir()
    db_dir = tmp_path / "db"
    schema = steps_dir / "schema.sql"
    schema.write_text(SCHEMA_SQL)

    class E:
        pass
    e = E()
    e.steps_dir, e.db_dir, e.schema = steps_dir, db_dir, schema
    e.locations = Locations(knowledge_db_dir=str(db_dir), private_data_dir=str(tmp_path / "data"),
                            notes_vault=str(tmp_path / "vault"), health_dir=str(tmp_path / "health"))
    e.live = str(db_dir / "knowledge.db")
    e.backups = db_dir / "backups"
    e.files, e.outputs = RecordingFiles(), NoOutputs()

    def make_app(clock=None):
        import clock as clocks
        registry = building.Registry(e.files, str(steps_dir), only=["a_step.py", "b_step.py"])
        return building.compose(e.locations, clock or clocks.SystemClock(), e.outputs, files=e.files,
                                code_dir_path=str(steps_dir), registry=registry, provenance=NoProvenance())
    e.make_app = make_app
    e.app = make_app()
    e.write_step = lambda name, body: (steps_dir / name).write_text(body)
    e.write_step("a_step.py", GOOD_STEP.format(v="a"))
    e.write_step("b_step.py", GOOD_STEP.format(v="b"))
    return e


def rows(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("SELECT v FROM t ORDER BY id")]
    finally:
        con.close()


def run_main(env, *args, app=None):
    build.main(env.locations, list(args), app=app or env.app)


def files_in(d):
    return sorted(p.name for p in d.iterdir() if p.is_file())


# ---- characterization: pinned on the old code first -------------------------

def test_build_into_empty_dir_creates_db(env):
    assert not env.db_dir.exists()
    run_main(env)
    assert rows(env.live) == ["a", "b"]
    assert not env.backups.exists()  # nothing to back up the first time


def test_rebuild_backs_up_old_db_and_replaces_it(env):
    run_main(env)
    env.write_step("b_step.py", GOOD_STEP.format(v="b2"))
    old_bytes = open(env.live, "rb").read()
    run_main(env)
    assert rows(env.live) == ["a", "b2"]
    baks = list(env.backups.iterdir())
    assert len(baks) == 1 and baks[0].name.startswith("knowledge.db.bak-")
    assert baks[0].read_bytes() == old_bytes


def test_prune_keeps_five_newest(env):
    env.backups.mkdir(parents=True)
    for i in range(8):
        (env.backups / f"knowledge.db.bak-2026010{i}T000000").write_text("x")
    (env.backups / "unrelated.txt").write_text("keep me")
    run_main(env)
    run_main(env)     # the second run has a live db to back up, which prunes
    kept = sorted(p.name for p in env.backups.iterdir())
    assert "unrelated.txt" in kept
    stale = [n for n in kept if n.endswith("T000000")]
    assert stale == ["knowledge.db.bak-20260104T000000", "knowledge.db.bak-20260105T000000",
                     "knowledge.db.bak-20260106T000000", "knowledge.db.bak-20260107T000000"]
    assert len(kept) == 1 + 5


def test_rebuild_prunes_to_five_backups(env):
    env.backups.mkdir(parents=True)
    for i in range(6):
        (env.backups / f"knowledge.db.bak-2026010{i}T000000").write_text("x")
    run_main(env)
    run_main(env)  # second run has a live db to back up
    assert len(list(env.backups.iterdir())) == 5


def test_check_does_not_touch_live_db_or_create_dir(env):
    run_main(env, "--check")
    assert not env.db_dir.exists()


def test_check_leaves_existing_live_db_untouched(env):
    run_main(env)
    before = open(env.live, "rb").read()
    env.write_step("a_step.py", GOOD_STEP.format(v="changed"))
    run_main(env, "--check")
    assert open(env.live, "rb").read() == before


class _FakeRunner:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def run_script(self, name, args=(), env=None):
        self.calls.append(("script", name, list(args), env))
        return self.code

    def run_module(self, module, args=(), env=None):
        self.calls.append(("module", module, list(args), env))
        return self.code


def _checkout_locations(private_dir="/checkout/A"):
    from locations import Locations
    return Locations(knowledge_db_dir="/x/db", private_data_dir="/x/data", notes_vault="/x/v",
                     health_dir="/x/h", private_dir=private_dir)


def test_the_build_command_runs_build_py_through_the_script_runner_port():
    import clock
    import scripts
    import scripts_service
    import script_runner
    runner = _FakeRunner(code=7)
    deps = scripts_service.ScriptsDeps(runner=runner, clock=clock.SystemClock(), locations=_checkout_locations())
    assert scripts_service.build(deps) == 7
    assert scripts_service.build(deps, check=True) == 7
    assert scripts_service.clean_concerts(deps) == 7
    env = {"KNOWLEDGE_PRIVATE_DIR": "/checkout/A"}
    assert runner.calls == [("script", "build.py", [], env), ("script", "build.py", ["--check"], env),
                            ("module", "client.clean_concerts_csv", [], env)]
    composed = scripts.compose(_checkout_locations(), clock.SystemClock())
    assert composed.deps.runner is script_runner      # the composition root binds the real adapter


def test_a_child_is_started_for_the_containers_configuration_not_the_environments(monkeypatch):
    """Container(locations=A, clock=fixed).scripts must not launch a child configured for whatever the
    environment holds (B): it hands over A's checkout and the fixed instant."""
    import clock
    import container
    import script_runner
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", "/checkout/B")
    monkeypatch.setenv("KNOWLEDGE_FROZEN_NOW", "2001-01-01T00:00:00+00:00")
    app = container.Container(_checkout_locations("/checkout/A"), clock.FixedClock("2026-10-03T08:00:00+00:00"))
    runner = _FakeRunner()
    object.__setattr__(app.scripts.deps, "runner", runner)       # swap the adapter, keep the real wiring
    app.scripts.build()
    (_kind, _name, _args, env), = runner.calls
    assert env == {"KNOWLEDGE_PRIVATE_DIR": "/checkout/A", "KNOWLEDGE_FROZEN_NOW": "2026-10-03T08:00:00+00:00"}
    # and the real adapter never lets the parent's own values through
    child = script_runner.child_environment(env)
    assert child["KNOWLEDGE_PRIVATE_DIR"] == "/checkout/A" and child["KNOWLEDGE_FROZEN_NOW"] == "2026-10-03T08:00:00+00:00"
    assert "KNOWLEDGE_PRIVATE_DIR" not in script_runner.child_environment({}, {"KNOWLEDGE_PRIVATE_DIR": "/checkout/B", "X": "1"})
    assert script_runner.child_environment({}, {"KNOWLEDGE_FROZEN_NOW": "x", "X": "1"}) == {"X": "1"}


def test_locations_that_did_not_come_from_a_checkout_cannot_start_a_script():
    import clock
    import scripts_service
    deps = scripts_service.ScriptsDeps(runner=_FakeRunner(), clock=clock.SystemClock(),
                                       locations=_checkout_locations(private_dir=None))
    with pytest.raises(ValueError, match="not read from a private checkout"):
        scripts_service.build(deps)
    assert deps.runner.calls == []


# ---- #17: failure safety -----------------------------------------------------

def test_failing_step_leaves_live_db_byte_identical_and_names_step(env, capsys):
    run_main(env)
    before = open(env.live, "rb").read()
    env.write_step("b_step.py", BAD_STEP)
    with pytest.raises(SystemExit) as ei:
        run_main(env)
    assert ei.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "b_step.py" in err and "boom" in err
    assert open(env.live, "rb").read() == before
    assert rows(env.live) == ["a", "b"]


def test_failing_step_leaves_no_temp_files(env):
    run_main(env)
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main(env)
    assert files_in(env.db_dir) == ["knowledge.db"]


def test_failure_on_first_ever_build_creates_no_live_db(env):
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main(env)
    assert not os.path.exists(env.live)
    assert files_in(env.db_dir) == []


def test_missing_external_export_mid_build_keeps_data(env, capsys):
    run_main(env)
    before = open(env.live, "rb").read()
    env.write_step("b_step.py", "def run(con):\n    open('/nonexistent/export.csv').read()\n")
    with pytest.raises(SystemExit):
        run_main(env)
    assert "b_step.py" in capsys.readouterr().err
    assert open(env.live, "rb").read() == before


def test_missing_step_file_is_a_named_failure(env, capsys):
    run_main(env)
    before = open(env.live, "rb").read()
    os.remove(env.steps_dir / "b_step.py")
    with pytest.raises(SystemExit):
        run_main(env)
    assert "b_step.py" in capsys.readouterr().err
    assert open(env.live, "rb").read() == before


def test_bad_schema_leaves_live_db(env, capsys):
    run_main(env)
    before = open(env.live, "rb").read()
    env.schema.write_text("CREATE TABLE oops (;\n")
    with pytest.raises(SystemExit):
        run_main(env)
    assert "schema" in capsys.readouterr().err
    assert open(env.live, "rb").read() == before


def test_second_build_right_after_failed_one_succeeds(env):
    run_main(env)
    env.write_step("b_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main(env)
    env.write_step("b_step.py", GOOD_STEP.format(v="fixed"))
    run_main(env)
    assert rows(env.live) == ["a", "fixed"]
    assert files_in(env.db_dir) == ["knowledge.db"]


def test_failed_build_does_not_back_up_or_prune(env):
    run_main(env)
    env.write_step("b_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main(env)
    assert not env.backups.exists() or list(env.backups.iterdir()) == []


def test_stale_temp_from_killed_build_is_ignored(env):
    run_main(env)
    (env.db_dir / "knowledge.db.building-stale").write_text("garbage from a killed build")
    run_main(env)
    assert rows(env.live) == ["a", "b"]


def test_check_failure_exits_nonzero_and_names_step(env, capsys):
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit) as ei:
        run_main(env, "--check")
    assert ei.value.code not in (0, None)
    assert "a_step.py" in capsys.readouterr().err


def test_check_uses_unique_cleaned_up_temp_path(env):
    run_main(env, "--check")
    run_main(env, "--check")
    seen = env.files.build_temps
    assert len(seen) == 2 and seen[0] != seen[1]
    assert "/tmp/knowledge_db_build_check.db" not in seen
    assert not any(os.path.exists(p) for p in seen)


def test_check_failure_cleans_up_temp(env):
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main(env, "--check")
    assert env.files.build_temps and not any(os.path.exists(p) for p in env.files.build_temps)


# ---- #17: swap semantics -----------------------------------------------------

def test_reader_holding_old_db_keeps_working_and_next_open_sees_new(env):
    run_main(env)
    reader = sqlite3.connect(env.live)
    cur = reader.execute("SELECT v FROM t ORDER BY id")
    assert cur.fetchone()[0] == "a"  # reader is mid-read on the old file
    env.write_step("b_step.py", GOOD_STEP.format(v="new"))
    run_main(env)
    assert [r[0] for r in cur.fetchall()] == ["b"]  # old snapshot still readable
    reader.close()
    assert rows(env.live) == ["a", "new"]


def test_swap_is_one_os_replace_within_the_same_directory(env, monkeypatch):  # the first build has no backup to copy
    calls = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (calls.append((s, d)), real(s, d))[1])
    run_main(env)
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == env.live and os.path.dirname(src) == os.path.dirname(dst)


def test_swapped_db_has_normal_file_mode_not_mkstemp_0600(env):
    run_main(env)
    umask = os.umask(0)
    os.umask(umask)
    assert os.stat(env.live).st_mode & 0o777 == 0o666 & ~umask


def test_backups_are_never_overwritten_even_under_a_fixed_clock(env):
    import clock
    fixed = clock.FixedClock("2026-10-03T08:00:00+00:00")
    app = env.make_app(fixed)
    run_main(env, app=app)                                    # first build: no live db to back up yet
    env.write_step("b_step.py", GOOD_STEP.format(v="second"))
    run_main(env, app=app)                                    # backs up build 1
    env.write_step("b_step.py", GOOD_STEP.format(v="third"))
    run_main(env, app=app)                                    # backs up build 2: same name, same second
    names = sorted(p.name for p in env.backups.iterdir())
    assert names == ["knowledge.db.bak-20261003T080000", "knowledge.db.bak-20261003T080000-2"]
    assert rows(str(env.backups / names[0])) == ["a", "b"] and rows(str(env.backups / names[1])) == ["a", "second"]
    assert rows(env.live) == ["a", "third"]


# ---- the build lane: backup lifecycle, failure recovery, normal-db invalidation, provenance --------------------

def test_twelve_rebuilds_under_one_frozen_clock_keep_distinct_backups_and_the_newest_five(env):
    """The names under one instant are `name`, `name-2`... `name-12`: as strings `-10` sorts before `-2`, so a
    string sort would prune the newest backups. Each backup holds the build it replaced."""
    import clock
    app = env.make_app(clock.FixedClock("2026-10-03T08:00:00+00:00"))
    for i in range(13):                          # build 0 has nothing to back up; builds 1..12 each back up the one before
        env.write_step("b_step.py", GOOD_STEP.format(v=f"build{i}"))
        run_main(env, app=app)
    base = "knowledge.db.bak-20261003T080000"
    expected = {f"{base}-{n}": f"build{n - 1}" for n in range(8, 13)}      # backups 8..12 (of 12) = the builds 7..11
    assert {p.name for p in env.backups.iterdir()} == set(expected)
    for name, v in expected.items():
        assert rows(str(env.backups / name)) == ["a", v]
    assert rows(env.live) == ["a", "build12"]


def test_a_backup_that_cannot_be_copied_aborts_the_swap_and_leaves_no_trace(env):
    run_main(env)
    before = open(env.live, "rb").read()
    env.write_step("b_step.py", GOOD_STEP.format(v="new"))

    class Failing(RecordingFiles):
        def copy(self, source, target):
            local_buildfiles.copy(source, target)       # a partial file exists when it fails
            raise OSError("disk full")
    env.files = Failing()
    with pytest.raises(SystemExit):
        run_main(env, app=env.make_app())
    assert open(env.live, "rb").read() == before
    assert files_in(env.db_dir) == ["knowledge.db"]
    assert not env.backups.exists() or list(env.backups.iterdir()) == []      # no empty file counting as the newest backup


def test_publication_failure_is_named_and_recovers(env, capsys):
    run_main(env)
    before = open(env.live, "rb").read()

    class NoReplace(RecordingFiles):
        def replace(self, source, target):
            if target == env.live:
                raise OSError("cross-device")
            local_buildfiles.replace(source, target)
    env.files = NoReplace()
    with pytest.raises(SystemExit):
        run_main(env, app=env.make_app())
    err = capsys.readouterr().err
    assert "publish" in err and "cross-device" in err
    assert open(env.live, "rb").read() == before
    assert files_in(env.db_dir) == ["knowledge.db"]
    assert [open(p, "rb").read() for p in env.backups.iterdir()] == [before]     # the backup taken first is a real, complete copy
    env.files = RecordingFiles()
    run_main(env, app=env.make_app())           # and the next build is unaffected
    assert rows(env.live) == ["a", "b"]


def test_normal_generation_failure_keeps_the_new_db_and_invalidates_the_old_normal_artifact(env, capsys):
    run_main(env)
    old_normal = env.db_dir / "knowledge-normal.db"
    old_normal.write_text("stale: may hold a fact that has since become private")
    env.write_step("b_step.py", GOOD_STEP.format(v="new"))

    class Boom(NoOutputs):
        def build_normal(self, full_path, directory):
            raise RuntimeError("leak test failed")
    env.outputs = Boom()
    with pytest.raises(SystemExit) as ei:
        run_main(env, app=env.make_app())
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "knowledge.db was rebuilt" in err and "leak test failed" in err and "removed" in err
    assert rows(env.live) == ["a", "new"]                    # the main build stays
    assert env.outputs.discarded == [str(env.db_dir)]       # and the service asked for the old normal artifact to go


def test_a_bad_privacy_rules_file_removes_the_stale_normal_db_with_the_real_outputs_adapter(env, capsys):
    """The rules are read BEFORE the atomic normal build starts, so normal_db's own cleanup never runs for this
    failure: the service has to invalidate the artifact itself."""
    run_main(env)
    data = env.locations.private_data_dir
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(data, "privacy_rules.json"), "w") as f:
        f.write("{not json")
    stale = env.db_dir / "knowledge-normal.db"
    stale.write_text("stale")
    (env.db_dir / "knowledge-normal.db-wal").write_text("stale sidecar")
    import clock
    real = building.compose(env.locations, clock.SystemClock(), build.NormalOutputs(local_buildfiles, env.locations),
                            files=local_buildfiles, code_dir_path=str(env.steps_dir), provenance=NoProvenance(),
                            registry=building.Registry(local_buildfiles, str(env.steps_dir), only=["a_step.py", "b_step.py"]))
    with pytest.raises(SystemExit):
        build.main(env.locations, [], app=real)
    assert not stale.exists() and not (env.db_dir / "knowledge-normal.db-wal").exists()
    assert "normal-only DB failed" in capsys.readouterr().err
    assert rows(env.live) == ["a", "b"]


def test_check_mode_normal_failure_exits_nonzero_and_discards_nothing_that_exists(env, capsys):
    run_main(env)
    before = open(env.live, "rb").read()
    stale = env.db_dir / "knowledge-normal.db"
    stale.write_text("previous normal db")

    class Boom(NoOutputs):
        def build_normal(self, full_path, directory):
            raise RuntimeError("rules unreadable")
    env.outputs = Boom()
    with pytest.raises(SystemExit) as ei:
        run_main(env, "--check", app=env.make_app())
    assert ei.value.code == 1
    assert "normal DB build failed: rules unreadable" in capsys.readouterr().err
    assert open(env.live, "rb").read() == before and stale.read_text() == "previous normal db"
    assert env.outputs.discarded == []
    assert not env.backups.exists()


def test_a_real_schema_build_records_provenance_rows_in_step_order(tmp_path):
    """Synthetic steps over the real schema.sql and the real provenance adapter: the build_info row, one
    build_inputs row per input by stable key, the step order, and the swap."""
    import clock
    import private_git
    import shutil
    import build_info_store
    code = tmp_path / "code"
    code.mkdir()
    shutil.copy(os.path.join(REPO, "schema.sql"), code / "schema.sql")
    for name, v in (("s1.py", "one"), ("s2.py", "two")):
        (code / name).write_text("def run(con, ctx):\n    con.execute(\"INSERT INTO publishers (name) VALUES ('%s')\")\n" % v)
    data = tmp_path / "data"
    data.mkdir()
    (data / "general_facts.json").write_text("[]")
    locations = Locations(knowledge_db_dir=str(tmp_path / "db"), private_data_dir=str(data))
    fixed = clock.FixedClock("2026-10-03T08:00:00+00:00")
    provenance = build_info_store.BuildProvenance(locations, str(code), private_git)
    app = building.compose(locations, fixed, NoOutputs(), files=local_buildfiles, code_dir_path=str(code),
                           registry=building.Registry(local_buildfiles, str(code), only=["s1.py", "s2.py"]),
                           provenance=provenance)
    outcome = app.rebuild()
    assert [(r.step, r.via) for r in outcome.steps] == [("s1.py", "file"), ("s2.py", "file")]
    con = sqlite3.connect(str(tmp_path / "db" / "knowledge.db"))
    try:
        assert [r[0] for r in con.execute("SELECT name FROM publishers ORDER BY id")] == ["one", "two"]
        info = con.execute("SELECT built_at, schema_version FROM build_info").fetchall()
        assert info == [("2026-10-03T08:00:00+00:00", con.execute("PRAGMA user_version").fetchone()[0])]
        keys = [r[0] for r in con.execute("SELECT input_key FROM build_inputs ORDER BY input_key")]
        assert keys == sorted(keys) and "input:data/general_facts.json" in keys and build_info_store.NOTES_KEY in keys
        states = dict(con.execute("SELECT input_key, state FROM build_inputs"))
        assert states["input:data/general_facts.json"] == "present" and states["input:data/entities.json"] == "missing"
    finally:
        con.close()


def test_callers_of_the_rebuild_handle_a_normal_failure_separately_from_a_build_failure():
    """A normal-db failure after publication is a different outcome (the new knowledge.db stays) from a failure
    before it. A caller that only catches BuildError would report the wrong state, so every caller of the use case's
    `rebuild`/`check` has to name NormalBuildError. Scans the code, not the tests."""
    import ast
    offenders = []
    for name in sorted(os.listdir(REPO)):
        if not name.endswith(".py") or name in ("build_service.py", "building.py"):
            continue
        text = open(os.path.join(REPO, name), encoding="utf-8").read()
        calls = [n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in ("rebuild", "check") and ast.unparse(n.func.value) in ("app", "build_service", "self.app")]
        if calls and "NormalBuildError" not in text:
            offenders.append(name)
    assert offenders == []
