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

SCHEMA_SQL = "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);\n"
GOOD_STEP = "def run(con):\n    con.execute(\"INSERT INTO t (v) VALUES ('{v}')\")\n    con.commit()\n"
BAD_STEP = ("def run(con):\n    con.execute(\"INSERT INTO t (v) VALUES ('half')\")\n    con.commit()\n"
            "    raise RuntimeError('boom')\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake pipeline in tmp_path; module globals redirected at it."""
    steps_dir = tmp_path / "steps"
    steps_dir.mkdir()
    db_dir = tmp_path / "db"
    schema = tmp_path / "schema.sql"
    schema.write_text(SCHEMA_SQL)
    monkeypatch.setattr(build, "HERE", str(steps_dir))
    monkeypatch.setattr(build, "schema_sql", lambda sources=None: schema.read_text())
    monkeypatch.setattr(build, "DB_DIR", str(db_dir))
    monkeypatch.setattr(build, "LIVE_DB", str(db_dir / "knowledge.db"))
    monkeypatch.setattr(build, "BACKUP_DIR", str(db_dir / "backups"))
    monkeypatch.setattr(build, "STEPS", ["a_step.py", "b_step.py"])
    # the fake schema has none of the real tables report() counts
    monkeypatch.setattr(build, "report", lambda path: None)
    # ...nor the facts tables the normal DB is built from; tests/test_normal_db.py covers that hook
    # ...nor the build_info tables; tests/test_build_info.py covers that hook
    monkeypatch.setattr(build, "record_build_info", lambda con: None)
    monkeypatch.setattr(build, "build_normal", lambda full, directory, rules=None: None)

    class E:
        pass
    e = E()
    e.steps_dir, e.db_dir, e.schema = steps_dir, db_dir, schema
    e.live = str(db_dir / "knowledge.db")
    e.backups = db_dir / "backups"
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


def run_main(*args):
    sys.argv = ["build.py", *args]
    build.main()


def files_in(d):
    return sorted(p.name for p in d.iterdir() if p.is_file())


# ---- characterization: pinned on the old code first -------------------------

def test_build_into_empty_dir_creates_db(env):
    assert not env.db_dir.exists()
    run_main()
    assert rows(env.live) == ["a", "b"]
    assert not env.backups.exists()  # nothing to back up the first time


def test_rebuild_backs_up_old_db_and_replaces_it(env):
    run_main()
    env.write_step("b_step.py", GOOD_STEP.format(v="b2"))
    old_bytes = open(env.live, "rb").read()
    run_main()
    assert rows(env.live) == ["a", "b2"]
    baks = list(env.backups.iterdir())
    assert len(baks) == 1 and baks[0].name.startswith("knowledge.db.bak-")
    assert baks[0].read_bytes() == old_bytes


def test_prune_keeps_five_newest(env):
    env.backups.mkdir(parents=True)
    for i in range(8):
        (env.backups / f"knowledge.db.bak-2026010{i}T000000").write_text("x")
    (env.backups / "unrelated.txt").write_text("keep me")
    build.prune_backups()
    assert sorted(p.name for p in env.backups.iterdir()) == [
        "knowledge.db.bak-20260103T000000", "knowledge.db.bak-20260104T000000",
        "knowledge.db.bak-20260105T000000", "knowledge.db.bak-20260106T000000",
        "knowledge.db.bak-20260107T000000", "unrelated.txt"]


def test_rebuild_prunes_to_five_backups(env):
    env.backups.mkdir(parents=True)
    for i in range(6):
        (env.backups / f"knowledge.db.bak-2026010{i}T000000").write_text("x")
    run_main()
    run_main()  # second run has a live db to back up
    assert len(list(env.backups.iterdir())) == 5


def test_check_does_not_touch_live_db_or_create_dir(env):
    run_main("--check")
    assert not env.db_dir.exists()


def test_check_leaves_existing_live_db_untouched(env):
    run_main()
    before = open(env.live, "rb").read()
    env.write_step("a_step.py", GOOD_STEP.format(v="changed"))
    run_main("--check")
    assert open(env.live, "rb").read() == before


def test_knowledge_py_build_dispatch_shells_out_to_build_py():
    src = open(os.path.join(REPO, "knowledge.py")).read()
    assert '"build.py"' in src and '"--check"' in src


# ---- #17: failure safety -----------------------------------------------------

def test_failing_step_leaves_live_db_byte_identical_and_names_step(env, capsys):
    run_main()
    before = open(env.live, "rb").read()
    env.write_step("b_step.py", BAD_STEP)
    with pytest.raises(SystemExit) as ei:
        run_main()
    assert ei.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "b_step.py" in err and "boom" in err
    assert open(env.live, "rb").read() == before
    assert rows(env.live) == ["a", "b"]


def test_failing_step_leaves_no_temp_files(env):
    run_main()
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main()
    assert files_in(env.db_dir) == ["knowledge.db"]


def test_failure_on_first_ever_build_creates_no_live_db(env):
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main()
    assert not os.path.exists(env.live)
    assert files_in(env.db_dir) == []


def test_missing_external_export_mid_build_keeps_data(env, capsys):
    run_main()
    before = open(env.live, "rb").read()
    env.write_step("b_step.py", "def run(con):\n    open('/nonexistent/export.csv').read()\n")
    with pytest.raises(SystemExit):
        run_main()
    assert "b_step.py" in capsys.readouterr().err
    assert open(env.live, "rb").read() == before


def test_missing_step_file_is_a_named_failure(env, capsys):
    run_main()
    before = open(env.live, "rb").read()
    os.remove(env.steps_dir / "b_step.py")
    with pytest.raises(SystemExit):
        run_main()
    assert "b_step.py" in capsys.readouterr().err
    assert open(env.live, "rb").read() == before


def test_bad_schema_leaves_live_db(env, capsys):
    run_main()
    before = open(env.live, "rb").read()
    env.schema.write_text("CREATE TABLE oops (;\n")
    with pytest.raises(SystemExit):
        run_main()
    assert "schema" in capsys.readouterr().err
    assert open(env.live, "rb").read() == before


def test_second_build_right_after_failed_one_succeeds(env):
    run_main()
    env.write_step("b_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main()
    env.write_step("b_step.py", GOOD_STEP.format(v="fixed"))
    run_main()
    assert rows(env.live) == ["a", "fixed"]
    assert files_in(env.db_dir) == ["knowledge.db"]


def test_failed_build_does_not_back_up_or_prune(env):
    run_main()
    env.write_step("b_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main()
    assert not env.backups.exists() or list(env.backups.iterdir()) == []


def test_stale_temp_from_killed_build_is_ignored(env):
    run_main()
    (env.db_dir / "knowledge.db.building-stale").write_text("garbage from a killed build")
    run_main()
    assert rows(env.live) == ["a", "b"]


def test_check_failure_exits_nonzero_and_names_step(env, capsys):
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit) as ei:
        run_main("--check")
    assert ei.value.code not in (0, None)
    assert "a_step.py" in capsys.readouterr().err


def test_check_uses_unique_cleaned_up_temp_path(env, monkeypatch):
    seen = []
    real = build.build

    def spy(target, *a, **k):
        seen.append(target)
        return real(target, *a, **k)
    monkeypatch.setattr(build, "build", spy)
    run_main("--check")
    run_main("--check")
    assert len(seen) == 2 and seen[0] != seen[1]
    assert "/tmp/knowledge_db_build_check.db" not in seen
    assert not any(os.path.exists(p) for p in seen)


def test_check_failure_cleans_up_temp(env, monkeypatch):
    seen = []
    real = build.build

    def spy(target, *a, **k):
        seen.append(target)
        return real(target, *a, **k)
    monkeypatch.setattr(build, "build", spy)
    env.write_step("a_step.py", BAD_STEP)
    with pytest.raises(SystemExit):
        run_main("--check")
    assert seen and not any(os.path.exists(p) for p in seen)


# ---- #17: swap semantics -----------------------------------------------------

def test_reader_holding_old_db_keeps_working_and_next_open_sees_new(env):
    run_main()
    reader = sqlite3.connect(env.live)
    cur = reader.execute("SELECT v FROM t ORDER BY id")
    assert cur.fetchone()[0] == "a"  # reader is mid-read on the old file
    env.write_step("b_step.py", GOOD_STEP.format(v="new"))
    run_main()
    assert [r[0] for r in cur.fetchall()] == ["b"]  # old snapshot still readable
    reader.close()
    assert rows(env.live) == ["a", "new"]


def test_swap_is_one_os_replace_within_the_same_directory(env, monkeypatch):
    calls = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (calls.append((s, d)), real(s, d))[1])
    run_main()
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == env.live and os.path.dirname(src) == os.path.dirname(dst)


def test_swapped_db_has_normal_file_mode_not_mkstemp_0600(env):
    run_main()
    umask = os.umask(0)
    os.umask(umask)
    assert os.stat(env.live).st_mode & 0o777 == 0o666 & ~umask
