"""paths.load() reads the TOML config file: $KNOWLEDGE_CONFIG if set, otherwise config.toml in the OS's per-user
config directory. A named file that is missing is an error, never a fallback; any setting can be overridden by its
KNOWLEDGE_<NAME> environment variable; unknown keys and missing required ones are refused with the file named.
"""
import os

import pytest

import paths
from config_files import write_config
from paths import ConfigError

NAMES = ("db_dir", "data_dir", "notes_vault", "health_dir")


@pytest.fixture
def default_dir(tmp_path, monkeypatch):
    """The per-user config directory, redirected into tmp_path so no test reads a developer's real one."""
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "knowledge"


def write(directory, marker, name="config.toml", **more):
    return write_config(str(directory / name), marker + "/db_dir", marker + "/data_dir", **more)


def test_the_per_user_default_is_used_when_the_env_var_is_unset(default_dir):
    write(default_dir, "default")
    assert paths.load().private_data_dir == "default/data_dir"
    assert paths.config_file() == str(default_dir / "config.toml")


def test_the_default_is_dot_config_on_macos_too_so_one_dotfile_source_path_serves_both(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.config_file() == str(tmp_path / ".config" / "knowledge" / "config.toml")


def test_the_env_var_names_the_file_and_works_without_a_default(default_dir, tmp_path, monkeypatch):
    elsewhere = write(tmp_path / "elsewhere", "envfile", name="mine.toml")
    monkeypatch.setenv("KNOWLEDGE_CONFIG", elsewhere)
    loc = paths.load()
    assert loc.private_data_dir == "envfile/data_dir" and loc.config_file == elsewhere.replace(os.sep, "/")


def test_the_env_var_wins_over_the_default(default_dir, tmp_path, monkeypatch):
    write(default_dir, "default")
    monkeypatch.setenv("KNOWLEDGE_CONFIG", write(tmp_path / "elsewhere", "envfile"))
    assert paths.load().private_data_dir == "envfile/data_dir"


def test_an_empty_env_var_counts_as_unset(default_dir, monkeypatch):
    write(default_dir, "default")
    monkeypatch.setenv("KNOWLEDGE_CONFIG", "")
    assert paths.load().private_data_dir == "default/data_dir"


def test_no_file_gives_a_clear_error_naming_the_default_and_the_env_var(default_dir):
    with pytest.raises(ConfigError) as e:
        paths.load()
    assert str(default_dir / "config.toml") in str(e.value) and "KNOWLEDGE_CONFIG" in str(e.value)


def test_an_env_var_naming_a_missing_file_is_an_error_not_a_silent_fallback(default_dir, tmp_path, monkeypatch):
    write(default_dir, "default")      # a fallback exists
    missing = str(tmp_path / "does-not-exist.toml")
    monkeypatch.setenv("KNOWLEDGE_CONFIG", missing)
    with pytest.raises(ConfigError, match="does-not-exist.toml"):
        paths.load()


def test_a_directory_in_place_of_the_file_is_an_error(default_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_CONFIG", str(tmp_path))
    with pytest.raises(ConfigError):
        paths.load()


def test_the_env_var_expands_a_leading_tilde_under_knowledge_home(default_dir, tmp_path, monkeypatch):
    write(tmp_path / "drive" / "cfg", "home")
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "drive"))
    monkeypatch.setenv("KNOWLEDGE_CONFIG", "~/cfg/config.toml")
    assert paths.load().private_data_dir == "home/data_dir"


# ------------------------------------------------------------------ what a file may hold

def test_invalid_toml_names_the_file(default_dir):
    default_dir.mkdir(parents=True)
    (default_dir / "config.toml").write_text("db_dir = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML") as e:
        paths.load()
    assert str(default_dir / "config.toml") in str(e.value)


def test_a_file_missing_a_required_setting_says_which(default_dir):
    default_dir.mkdir(parents=True)
    (default_dir / "config.toml").write_text('db_dir = "/db"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="data_dir"):
        paths.load()


@pytest.mark.parametrize("line", ['BODYBUILDING_VAULT = "/old"', 'notes_vualt = "/typo"'])
def test_unknown_keys_are_refused_so_a_typo_or_an_old_name_cannot_pass_silently(default_dir, line):
    write(default_dir, "x", extra=line + "\n")      # no [client_inputs] table, so the line is a top-level key
    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        paths.load()


def test_setting_names_are_case_insensitive_like_their_environment_variables(default_dir):
    write(default_dir, "x", extra='NOTES_VAULT = "/upper"\n')
    assert paths.load().notes_vault == "/upper"


def test_a_value_of_the_wrong_type_is_refused(default_dir):
    default_dir.mkdir(parents=True)
    (default_dir / "config.toml").write_text('db_dir = "/db"\ndata_dir = 5\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="data_dir"):
        paths.load()


def test_the_file_is_data_not_code(default_dir):
    default_dir.mkdir(parents=True)
    marker = default_dir / "ran"
    (default_dir / "config.toml").write_text(f'db_dir = "/db"\ndata_dir = "/d"\n__import__("pathlib").Path({str(marker)!r}).touch()\n',
                                             encoding="utf-8")
    with pytest.raises(ConfigError):
        paths.load()
    assert not marker.exists()


# ------------------------------------------------------------------ environment overrides

@pytest.mark.parametrize("setting, value, field", [
    ("KNOWLEDGE_DB_DIR", "/env/db", "knowledge_db_dir"), ("KNOWLEDGE_DATA_DIR", "/env/data", "private_data_dir"),
    ("KNOWLEDGE_NOTES_VAULT", "/env/vault", "notes_vault"), ("KNOWLEDGE_HEALTH_DIR", "/env/health", "health_dir")])
def test_a_setting_can_be_overridden_by_its_environment_variable(default_dir, monkeypatch, setting, value, field):
    write(default_dir, "file", notes_vault="/file/vault", health_dir="/file/health")
    monkeypatch.setenv(setting, value)
    assert getattr(paths.load(), field) == value


def test_client_sources_can_be_overridden_from_the_environment_as_json(default_dir, monkeypatch):
    write(default_dir, "file", client_sources=["claims"])
    monkeypatch.setenv("KNOWLEDGE_CLIENT_SOURCES", '["scrobbles"]')
    assert paths.load().client_sources == ("scrobbles",)


def test_the_other_knowledge_variables_are_not_settings(default_dir, monkeypatch):
    """KNOWLEDGE_HOME and KNOWLEDGE_FROZEN_NOW share the prefix; they must not be read as unknown settings."""
    write(default_dir, "file")
    monkeypatch.setenv("KNOWLEDGE_HOME", "/somewhere")
    monkeypatch.setenv("KNOWLEDGE_FROZEN_NOW", "2026-01-01T00:00:00+00:00")
    assert paths.load().private_data_dir == "file/data_dir"


# ------------------------------------------------------------------ client_sources and client_inputs

def sources_of(default_dir, **settings):
    write(default_dir, "x", **settings)
    loc = paths.load()
    return loc.client_sources, loc.client_input("CONCERTS_CSV")


def test_client_sources_are_taken_as_written(default_dir):
    assert sources_of(default_dir, client_sources=["scrobbles", "claims"],
                      client_inputs={"CONCERTS_CSV": "c.csv"}) == (("scrobbles", "claims"), "c.csv")


def test_inputs_without_client_sources_enable_nothing(default_dir):
    """Nothing is guessed from which inputs happen to be defined: the core builds alone until sources are listed."""
    assert sources_of(default_dir, client_inputs={"CONCERTS_CSV": "c.csv"}) == ((), "c.csv")


def test_a_config_without_client_sources_builds_the_core_only(default_dir):
    assert sources_of(default_dir) == ((), None)


def test_an_explicitly_empty_list_means_the_core_only(default_dir):
    assert sources_of(default_dir, client_sources=[], client_inputs={"CONCERTS_CSV": "c.csv"}) == ((), "c.csv")


def test_client_input_names_are_case_insensitive_in_the_file_and_empty_values_are_dropped(default_dir):
    default_dir.mkdir(parents=True)
    (default_dir / "config.toml").write_text(
        'db_dir = "/db"\ndata_dir = "/d"\n[client_inputs]\nconcerts_csv = "c.csv"\nRYM_EXPORT_CSV = ""\n', encoding="utf-8")
    loc = paths.load()
    assert dict(loc.client_inputs) == {"CONCERTS_CSV": "c.csv"} and loc.client_input("RYM_EXPORT_CSV") is None


# ------------------------------------------------------------------ one synced file, several machines

def test_every_path_may_start_with_a_tilde_including_the_data_dir(default_dir, tmp_path, monkeypatch):
    default_dir.mkdir(parents=True)
    (default_dir / "config.toml").write_text('db_dir = "~/db"\ndata_dir = "~/private/data"\nnotes_vault = "~/vault"\n',
                                             encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "machine-a"))
    a = paths.load()
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path / "machine-b"))
    b = paths.load()
    assert a.private_data_dir.endswith("machine-a/private/data") and b.private_data_dir.endswith("machine-b/private/data")
    assert (a.notes_vault_stored, b.notes_vault_stored) == ("~/vault", "~/vault")     # the stored form is machine-free


def test_a_local_file_beside_the_config_overrides_it_and_the_rest_is_kept(default_dir):
    write(default_dir, "shared", notes_vault="~/shared-vault", client_inputs={"CONCERTS_CSV": "~/c.csv", "RYM_EXPORT_CSV": "~/r.csv"})
    (default_dir / "config.local.toml").write_text(
        'notes_vault = "D:/Notes"\n[client_inputs]\nconcerts_csv = "D:/c.csv"\n', encoding="utf-8")
    loc = paths.load()
    assert loc.notes_vault == "D:/Notes"                                      # overridden on this machine
    assert loc.client_input("CONCERTS_CSV") == "D:/c.csv"                     # one key of a table overridden ...
    assert loc.client_input_stored("RYM_EXPORT_CSV") == "~/r.csv"             # ... the table's other keys kept
    assert loc.private_data_dir == "shared/data_dir"                          # untouched settings come from the shared file


def test_the_environment_wins_over_the_local_file(default_dir, monkeypatch):
    write(default_dir, "shared")
    (default_dir / "config.local.toml").write_text('db_dir = "/local/db"\n', encoding="utf-8")
    assert paths.load().knowledge_db_dir == "/local/db"
    monkeypatch.setenv("KNOWLEDGE_DB_DIR", "/env/db")
    assert paths.load().knowledge_db_dir == "/env/db"


def test_a_local_file_alone_is_not_a_config(default_dir):
    default_dir.mkdir(parents=True)
    (default_dir / "config.local.toml").write_text('db_dir = "/db"\ndata_dir = "/d"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="config file not found"):
        paths.load()


def test_the_local_file_follows_a_file_named_by_the_env_var(default_dir, tmp_path, monkeypatch):
    main = write(tmp_path / "elsewhere", "shared", name="mine.toml")
    (tmp_path / "elsewhere" / "mine.local.toml").write_text('health_dir = "/local/health"\n', encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_CONFIG", main)
    assert paths.load().health_dir == "/local/health"
    assert paths.local_override_file(main) == str(tmp_path / "elsewhere" / "mine.local.toml")


def test_a_bad_local_file_is_refused_like_the_main_one(default_dir):
    write(default_dir, "shared")
    (default_dir / "config.local.toml").write_text('notes_vualt = "/typo"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        paths.load()


def test_only_the_two_process_adapters_name_the_config_variable_and_they_agree():
    """The variable is the protocol between the adapter that starts a child (script_runner) and the loader that reads
    it (paths). Neither the model ring, the use cases nor the composition root may mention it: they pass a config file."""
    import glob
    import script_runner
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spelled = sorted(os.path.relpath(p, root) for p in glob.glob(os.path.join(root, "**", "*.py"), recursive=True)
                     if "/tests/" not in p and '"KNOWLEDGE_CONFIG"' in open(p, encoding="utf-8").read())
    assert spelled == ["paths.py", "script_runner.py"]
    assert script_runner.CONFIG_ENV_VAR == paths.CONFIG_ENV_VAR
