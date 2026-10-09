"""#47: build info and an inputs manifest recorded in every built db.

The schema-version rule is enforced here: tests fail when schema.sql changes without a bump of
`PRAGMA user_version`. Temp dirs and temp git repos only.
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import types

import pytest

import build_info_store as bi
import private_git
from test_add_fact import REPO

# Bump rule: when this fails, schema.sql changed. Bump `PRAGMA user_version` in schema.sql, then update BOTH
# constants below (the version and the fingerprint the failure message prints).
EXPECTED_SCHEMA_VERSION = 2
EXPECTED_SCHEMA_FINGERPRINT = '4d7b04b7e5ec57e0'


def schema_text():
    return open(os.path.join(REPO, "schema.sql"), encoding="utf-8").read()


def fingerprint(sql):
    """Hash of the schema with comments and whitespace removed, so only real changes count."""
    body = re.sub(r"--[^\n]*", "", sql)
    return hashlib.sha256(re.sub(r"\s+", " ", body).strip().encode()).hexdigest()[:16]


def test_schema_version_is_bumped_whenever_the_schema_changes():
    con = sqlite3.connect(":memory:")
    con.executescript(schema_text())
    version = con.execute("PRAGMA user_version").fetchone()[0]
    got = fingerprint(schema_text())
    assert (version, got) == (EXPECTED_SCHEMA_VERSION, EXPECTED_SCHEMA_FINGERPRINT), (
        f"schema.sql changed. Bump `PRAGMA user_version` in schema.sql (now {version}) and set "
        f"EXPECTED_SCHEMA_VERSION = <new version> and EXPECTED_SCHEMA_FINGERPRINT = {got!r} in tests/test_build_info.py")


def test_comment_and_whitespace_edits_do_not_change_the_fingerprint():
    assert fingerprint("CREATE TABLE t (a INT); -- x") == fingerprint("CREATE   TABLE t\n(a INT);\n-- y\n")
    assert fingerprint("CREATE TABLE t (a INT);") != fingerprint("CREATE TABLE t (a INT, b INT);")


# ------------------------------------------------------------------ fixtures

def fake_paths(tmp_path, present=("vault", "csv")):
    root = tmp_path / "in"
    (root / "vault").mkdir(parents=True)
    (root / "data").mkdir()
    ns = types.SimpleNamespace(BODYBUILDING_VAULT=str(root / "vault"), CONCERTS_CSV=str(root / "concerts.csv"),
                               RYM_EXPORT_CSV=str(root / "rym.csv"), SCROBBLES_JSON=str(root / "scrobbles.json"),
                               PRIVATE_DATA_DIR=str(root / "data"))
    if "vault" in present:
        (root / "vault" / "bodybuilding.db").write_bytes(b"vault-db-bytes")
    if "csv" in present:
        (root / "concerts.csv").write_text("a,b\n1,2\n")
    (root / "data" / "pilot_facts.json").write_text("[]")
    return ns


def new_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(schema_text())
    return con


def inputs(con):
    return {r["input_key"]: tuple(r)[2:7] for r in con.execute("SELECT * FROM build_inputs ORDER BY input_key")}


# ------------------------------------------------------------------ recording

def test_record_writes_one_info_row_and_a_manifest_with_stable_keys(tmp_path):
    paths = fake_paths(tmp_path)
    con = new_db()
    info_id = bi.record(con, paths, str(tmp_path), str(tmp_path))
    row = con.execute("SELECT * FROM build_info").fetchone()
    assert row["id"] == info_id and row["schema_version"] == EXPECTED_SCHEMA_VERSION
    assert row["python_version"] == sys.version.split()[0] and row["sqlite_version"] == sqlite3.sqlite_version
    assert re.match(r"\d{4}-\d\d-\d\dT", row["built_at"])
    keys = [r["input_key"] for r in con.execute("SELECT input_key FROM build_inputs")]
    assert all(k.startswith("input:") for k in keys) and "input:vault-db" in keys and "input:data/pilot_facts.json" in keys
    assert not any(str(tmp_path) in k for k in keys)                                 # keys, never paths
    assert not any(str(tmp_path) in str(v) for r in con.execute("SELECT * FROM build_inputs") for v in tuple(r))


def test_present_inputs_are_hashed_and_missing_ones_are_recorded_not_errors(tmp_path):
    paths = fake_paths(tmp_path)
    con = new_db()
    bi.record(con, paths, str(tmp_path), str(tmp_path))
    got = inputs(con)
    state, sha, size, mtime, _ = got["input:vault-db"]
    assert (state, sha, size) == ("present", hashlib.sha256(b"vault-db-bytes").hexdigest(), 14) and mtime
    assert got["input:concerts-csv"][0] == "present"
    for key in ("input:rym-export-csv", "input:scrobbles-json", "input:data/entities.json", "input:data/subjects.json"):
        assert got[key][:4] == ("missing", None, None, None), key
    assert got["input:vault-source-notes"][0] == "missing"                          # no sources in this db


def test_a_missing_vault_directory_is_a_missing_input_too(tmp_path):
    paths = fake_paths(tmp_path, present=())
    paths.BODYBUILDING_VAULT = str(tmp_path / "does" / "not" / "exist")
    con = new_db()
    bi.record(con, paths, str(tmp_path), str(tmp_path))
    assert inputs(con)["input:vault-db"][0] == "missing"


def test_two_records_over_identical_inputs_give_identical_manifests(tmp_path):
    paths = fake_paths(tmp_path)
    a, b = new_db(), new_db()
    bi.record(a, paths, str(tmp_path), str(tmp_path))
    time.sleep(0.01)
    bi.record(b, paths, str(tmp_path), str(tmp_path))
    strip = lambda con: [tuple(r)[1:6] for r in con.execute("SELECT * FROM build_inputs ORDER BY input_key")]   # noqa: E731
    assert strip(a) == strip(b) and len(strip(a)) == len(bi.DATA_FILES) + 5


def test_changing_an_input_changes_only_its_row(tmp_path):
    paths = fake_paths(tmp_path)
    a, b = new_db(), new_db()
    bi.record(a, paths, str(tmp_path), str(tmp_path))
    (tmp_path / "in" / "concerts.csv").write_text("a,b\n1,3\n")
    bi.record(b, paths, str(tmp_path), str(tmp_path))
    assert bi.compare(bi.latest(a), bi.latest(b)) == [
        {"key": "input:concerts-csv", "change": "changed", "a_sha256": inputs(a)["input:concerts-csv"][1], "b_sha256": inputs(b)["input:concerts-csv"][1]}]


def test_source_notes_manifest_uses_the_stored_hashes_and_is_order_independent():
    con = new_db()
    for i, (name, sha) in enumerate([("b.md", "b" * 64), ("a.md", "a" * 64)], start=1):
        con.execute("INSERT INTO sources (id, name, source_type, origin_path, content_sha256, size_bytes, file_mtime) VALUES (?, 'S', 'primary', ?, ?, 10, ?)",
                    (i, f"/v/sources/{name}", sha, f"2026-0{i}-01T00:00:00+00:00"))
    m = bi._notes_manifest(con)
    assert m["state"] == "present" and m["size_bytes"] == 20 and m["file_mtime"] == "2026-02-01T00:00:00+00:00"
    assert m["sha256"] == hashlib.sha256(("a.md:" + "a" * 64 + "\nb.md:" + "b" * 64).encode()).hexdigest()


# ------------------------------------------------------------------ git state

def git(cwd, *a):
    subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


def test_describe_repo_clean_dirty_not_a_repo_and_empty_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert private_git.describe_repo(str(plain)) == {"commit": None, "dirty": None}
    assert private_git.describe_repo(str(tmp_path / "nope")) == {"commit": None, "dirty": None}
    repo = tmp_path / "repo"
    repo.mkdir()
    git(str(repo), "init", "-q")
    git(str(repo), "config", "user.name", "T")
    git(str(repo), "config", "user.email", "t@example.com")
    assert private_git.describe_repo(str(repo))["commit"] is None                     # no commit yet
    (repo / "f").write_text("x")
    git(str(repo), "add", "-A")
    git(str(repo), "commit", "-q", "-m", "m")
    d = private_git.describe_repo(str(repo))
    assert re.fullmatch(r"[0-9a-f]{40}", d["commit"]) and d["dirty"] is False
    (repo / "g").write_text("y")
    assert private_git.describe_repo(str(repo))["dirty"] is True
    sub = repo / "sub"
    sub.mkdir()
    assert private_git.describe_repo(str(sub))["commit"] == d["commit"]               # works from a subdirectory


def test_record_stores_the_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(str(repo), "init", "-q")
    git(str(repo), "config", "user.name", "T")
    git(str(repo), "config", "user.email", "t@example.com")
    (repo / "f").write_text("x")
    git(str(repo), "add", "-A")
    git(str(repo), "commit", "-q", "-m", "m")
    con = new_db()
    bi.record(con, fake_paths(tmp_path), str(repo), str(tmp_path / "in"))
    r = con.execute("SELECT * FROM build_info").fetchone()
    assert re.fullmatch(r"[0-9a-f]{40}", r["code_commit"]) and r["code_dirty"] == 0 and r["private_commit"] is None and r["private_dirty"] is None


# ------------------------------------------------------------------ reading and comparing

def test_latest_returns_the_newest_build_or_none_and_old_dbs_raise():
    con = new_db()
    assert bi.latest(con) is None
    con.execute("INSERT INTO build_info (id, built_at, schema_version) VALUES (1, 't1', 1), (2, 't2', 1)")
    con.execute("INSERT INTO build_inputs (build_id, input_key, state, read_at) VALUES (2, 'input:x', 'missing', 't')")
    got = bi.latest(con)
    assert got["build"]["built_at"] == "t2" and [i["input_key"] for i in got["inputs"]] == ["input:x"]
    old = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        bi.latest(old)


def test_compare_reports_every_kind_of_difference():
    def build(*rows):
        return {"inputs": [{"input_key": k, "state": s, "sha256": h} for k, s, h in rows]}
    a = build(("k1", "present", "a"), ("k2", "present", "b"), ("k3", "present", "c"), ("k4", "missing", None), ("k5", "present", "e"))
    b = build(("k1", "present", "a"), ("k2", "present", "B"), ("k3", "missing", None), ("k4", "present", "d"), ("k6", "present", "f"))
    assert [(d["key"], d["change"]) for d in bi.compare(a, b)] == [
        ("k2", "changed"), ("k3", "now-missing"), ("k4", "now-present"), ("k5", "removed"), ("k6", "added")]
    assert bi.compare(a, a) == [] and bi.compare(None, None) == []


# ------------------------------------------------------------------ CLI

def cli_env(tmp_path):
    from test_cli_wiring import Env
    return Env(tmp_path)


def seed(env, sha):
    env.sql("INSERT INTO build_info (id, built_at, schema_version, code_commit, code_dirty, python_version, sqlite_version) "
            "VALUES (1, '2026-10-08T00:00:00+00:00', 1, ?, 1, '3.13.5', '3.45')", "c" * 40)
    env.sql("INSERT INTO build_inputs VALUES (1, 'input:concerts-csv', 'present', ?, 12, '2026-01-01T00:00:00+00:00', '2026-10-08T00:00:00+00:00')", sha)
    env.sql("INSERT INTO build_inputs VALUES (1, 'input:scrobbles-json', 'missing', NULL, NULL, NULL, '2026-10-08T00:00:00+00:00')")


def test_cli_build_info_text_and_json(tmp_path):
    env = cli_env(tmp_path)
    r = env.cli("build-info")
    assert r.returncode == 1 and "recorded no build info" in r.stderr
    seed(env, "a" * 64)
    out = env.cli("build-info").stdout
    assert "schema v1" in out and "python 3.13.5" in out and "(uncommitted changes)" in out and "knowledge-private: not in git" in out
    assert "input:concerts-csv: aaaaaaaaaaaa  12 bytes" in out and "input:scrobbles-json: missing" in out
    j = json.loads(env.cli("build-info", "--json").stdout)
    assert j["build"]["code_commit"] == "c" * 40 and len(j["inputs"]) == 2 and j["changed_inputs"] is None


def test_cli_build_info_compare_exit_codes(tmp_path):
    env = cli_env(tmp_path)
    seed(env, "a" * 64)
    other = cli_env(tmp_path / "other")
    seed(other, "a" * 64)
    r = env.cli("build-info", "--compare", other.db)
    assert r.returncode == 0 and r.stdout.strip() == "No input differs."
    other.sql("UPDATE build_inputs SET sha256 = ? WHERE input_key = 'input:concerts-csv'", "b" * 64)
    r = env.cli("build-info", "--compare", other.db)
    assert r.returncode == 1 and r.stdout.strip() == "input:concerts-csv: changed"
    j = json.loads(env.cli("build-info", "--compare", other.db, "--json").stdout)
    assert j["changed_inputs"][0]["change"] == "changed"
    r = env.cli("build-info", "--compare", str(tmp_path / "nope.db"))
    assert r.returncode == 1 and "doesn't exist" in r.stderr


# ------------------------------------------------------------------ normal-only DB

def test_normal_db_has_built_at_and_schema_version_only():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        con = sqlite3.connect(path)
        assert [c[1] for c in con.execute("PRAGMA table_info(build_info)")] == ["id", "built_at", "schema_version"]
        assert tuple(con.execute("SELECT built_at, schema_version FROM build_info").fetchone()) == ("2026-10-08T00:00:00+00:00", 1)
        assert "build_inputs" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        raw = open(path, "rb").read()
        for key in ("code commit", "private commit", "build input key"):
            assert leak_test.MARKERS[key].encode() not in raw, key
        assert not leak_test.scan_against_full(path, full)
        con.execute("UPDATE build_info SET built_at = ?", (leak_test.MARKERS["code commit"],))
        con.commit()
        con.close()
        assert leak_test.scan_against_full(path, full)


# ------------------------------------------------------------------ cost

def test_hashing_a_scrobbles_sized_input_is_fast(tmp_path, capsys):
    """The scrobbles export is the biggest input. Measure a file of that order (≈64 MB) and require it to stay well under a second or two."""
    p = tmp_path / "big.json"
    with open(p, "wb") as f:
        chunk = os.urandom(1 << 20)
        for _ in range(64):
            f.write(chunk)
    start = time.perf_counter()
    fp = bi.fixity_store.fingerprint(str(p))
    took = time.perf_counter() - start
    with capsys.disabled():
        print(f"\n[#47] hashed a {fp['size_bytes'] / 1e6:.0f} MB input in {took:.2f} s")
    assert fp["file_state"] == "present" and took < 5
