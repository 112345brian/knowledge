"""paths.py resolves the private companion repo: KNOWLEDGE_PRIVATE_DIR if set,
otherwise the sibling ../knowledge-private. Every test copies paths.py into a
temp tree, so none of them touch the real private repo.
"""
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = ["KNOWLEDGE_DB_DIR", "BODYBUILDING_VAULT", "HEALTH_DIR", "CONCERTS_CSV",
         "RYM_EXPORT_CSV", "SCROBBLES_JSON", "PRIVATE_DATA_DIR"]


def write_local_paths(directory, marker):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "local_paths.py"), "w") as f:
        for n in NAMES:
            f.write(f"{n} = {marker + '/' + n!r}\n")


def make_tree(tmp_path):
    """tmp/knowledge/paths.py, the shape of the real checkout."""
    code = tmp_path / "knowledge"
    code.mkdir()
    shutil.copy(os.path.join(REPO, "paths.py"), code / "paths.py")
    return code


def run_import(code_dir, env_var=None):
    env = {k: v for k, v in os.environ.items() if k != "KNOWLEDGE_PRIVATE_DIR"}
    if env_var is not None:
        env["KNOWLEDGE_PRIVATE_DIR"] = env_var
    script = "import paths; print(paths.PRIVATE_DATA_DIR)"
    return subprocess.run([sys.executable, "-c", script], cwd=code_dir, env=env,
                          capture_output=True, text=True)


def test_sibling_is_used_when_env_var_unset(tmp_path):
    code = make_tree(tmp_path)
    write_local_paths(tmp_path / "knowledge-private", "sibling")
    r = run_import(code)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "sibling/PRIVATE_DATA_DIR"


def test_env_var_works_without_a_sibling(tmp_path):
    code = make_tree(tmp_path)
    write_local_paths(tmp_path / "elsewhere", "envdir")
    r = run_import(code, env_var=str(tmp_path / "elsewhere"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "envdir/PRIVATE_DATA_DIR"


def test_env_var_wins_over_sibling(tmp_path):
    code = make_tree(tmp_path)
    write_local_paths(tmp_path / "knowledge-private", "sibling")
    write_local_paths(tmp_path / "elsewhere", "envdir")
    r = run_import(code, env_var=str(tmp_path / "elsewhere"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "envdir/PRIVATE_DATA_DIR"


def test_empty_env_var_counts_as_unset(tmp_path):
    code = make_tree(tmp_path)
    write_local_paths(tmp_path / "knowledge-private", "sibling")
    r = run_import(code, env_var="")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "sibling/PRIVATE_DATA_DIR"


def test_neither_location_gives_a_clear_error(tmp_path):
    code = make_tree(tmp_path)
    r = run_import(code)
    assert r.returncode != 0
    assert "ModuleNotFoundError" in r.stderr
    assert "KNOWLEDGE_PRIVATE_DIR" in r.stderr
    assert str(tmp_path / "knowledge-private") in r.stderr


def test_env_var_pointing_at_a_missing_dir_is_an_error_not_a_silent_fallback(tmp_path):
    code = make_tree(tmp_path)
    write_local_paths(tmp_path / "knowledge-private", "sibling")  # a fallback exists
    missing = str(tmp_path / "does-not-exist")
    r = run_import(code, env_var=missing)
    assert r.returncode != 0
    assert missing in r.stderr


def test_env_var_pointing_at_a_dir_without_local_paths_is_an_error(tmp_path):
    code = make_tree(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run_import(code, env_var=str(empty))
    assert r.returncode != 0
    assert str(empty) in r.stderr


# ------------------------------------------------------------------ CLIENT_SOURCES

def sources_of(tmp_path, extra_lines, names=NAMES):
    code = make_tree(tmp_path)
    private = tmp_path / "knowledge-private"
    private.mkdir()
    with open(private / "local_paths.py", "w") as f:
        for n in names:
            f.write(f"{n} = {n.lower()!r}\n")
        f.write(extra_lines)
    env = dict(os.environ)
    env.pop("KNOWLEDGE_PRIVATE_DIR", None)
    r = subprocess.run([sys.executable, "-c", "import paths; print(paths.CLIENT_SOURCES, paths.CLIENT_SOURCES_IMPLICIT, paths.CONCERTS_CSV)"],
                       cwd=code, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_client_sources_are_taken_as_written(tmp_path):
    assert sources_of(tmp_path, "CLIENT_SOURCES = ('scrobbles', 'claims')\n") == "('scrobbles', 'claims') False concerts_csv"


def test_a_config_with_the_music_inputs_but_no_client_sources_is_assumed_to_want_everything(tmp_path):
    out = sources_of(tmp_path, "")
    assert out == "('concerts', 'ratings', 'scrobbles', 'measurements', 'claims') True concerts_csv"


def test_a_config_without_music_inputs_and_without_client_sources_builds_the_core_only(tmp_path):
    core_names = ["KNOWLEDGE_DB_DIR", "BODYBUILDING_VAULT", "HEALTH_DIR", "PRIVATE_DATA_DIR"]
    assert sources_of(tmp_path, "", names=core_names) == "() False None"


def test_an_explicitly_empty_list_means_the_core_only_even_with_music_inputs(tmp_path):
    assert sources_of(tmp_path, "CLIENT_SOURCES = ()\n") == "() False concerts_csv"


def test_the_assumed_list_is_every_known_source():
    import build_rules
    text = open(os.path.join(REPO, "paths.py")).read()
    assert '("concerts", "ratings", "scrobbles", "measurements", "claims")' in text
    assert tuple(build_rules.CLIENT_SOURCES) == ("concerts", "ratings", "scrobbles", "measurements", "claims")
