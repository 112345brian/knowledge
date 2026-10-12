"""OS-agnostic path handling: `Locations` joins and expands strings the same on every host, and `~` can be pointed
at another root (KNOWLEDGE_HOME) without changing the form the database stores."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import locations  # noqa: E402
import paths  # noqa: E402
from locations import Locations, canonical_path, expand_home, join_path  # noqa: E402

WIN = "C:" + chr(92) + "Users" + chr(92) + "x"
BS = chr(92)


@pytest.mark.parametrize("directory, names, expected", [
    ("/data", ("a.json",), "/data/a.json"),                      # a POSIX fake stays POSIX on every host
    ("/data/", ("a.json",), "/data/a.json"),
    ("/", ("a",), "/a"),
    (WIN, ("a.json",), WIN + BS + "a.json"),                      # a native Windows path stays native
    (WIN + BS, ("sub/a.json",), WIN + BS + "sub" + BS + "a.json"),
    ("C:/Users/x", ("a",), "C:/Users/x/a"),
    ("", ("a",), "a"),
    ("/v", ("sources", "n.md"), "/v/sources/n.md"),
])
def test_join_path_keeps_the_separator_the_directory_already_uses(directory, names, expected):
    assert join_path(directory, *names) == expected


@pytest.mark.parametrize("path, expected", [
    ("~", "/h"), ("~/MEGA/v", "/h/MEGA/v"), ("~" + BS + "MEGA", "/h/MEGA"),
    ("~other/x", "~other/x"), ("/abs/~/x", "/abs/~/x"), ("rel/x", "rel/x"),
])
def test_expand_home_only_rewrites_a_leading_tilde(path, expected):
    assert expand_home(path, "/h") == expected


def test_knowledge_home_redirects_tilde_and_unset_means_the_os_home(monkeypatch, tmp_path):
    monkeypatch.delenv(locations.HOME_ENV_VAR, raising=False)
    assert paths.expand("~/MEGA") == os.path.expanduser("~/MEGA")
    monkeypatch.setenv(locations.HOME_ENV_VAR, str(tmp_path))
    assert paths.expand("~/MEGA/x") == join_path(str(tmp_path), "MEGA/x")
    assert paths.expand("/already/absolute") == "/already/absolute"


def test_the_stored_form_does_not_follow_knowledge_home(monkeypatch, tmp_path):
    """The database keeps `~/...` as local_paths.py wrote it; only reads see the redirected root."""
    monkeypatch.setenv(locations.HOME_ENV_VAR, str(tmp_path))
    loc = Locations(knowledge_db_dir="/db", private_data_dir="/data", notes_vault=paths.expand("~/MEGA/v"),
                    notes_vault_written="~/MEGA/v")
    assert loc.notes_vault_stored == "~/MEGA/v" and str(tmp_path) in loc.notes_vault


def test_locations_joins_are_host_independent():
    loc = Locations(knowledge_db_dir="/db", private_data_dir="/data")
    assert (loc.db_path, loc.backup_dir, loc.data_path) == ("/db/knowledge.db", "/db/backups", "/data/general_facts.json")
    assert loc.in_data_dir("x.json", "/other") == "/other/x.json"
    with pytest.raises(ValueError, match="NOTES_VAULT"):
        loc.in_notes_vault("sources")
    assert Locations("/db", "/data", notes_vault="/v").in_notes_vault("sources") == "/v/sources"


@pytest.mark.parametrize("path, expected", [
    (WIN, "C:/Users/x"),                                   # drive letter
    ("C:/a" + BS + "b", "C:/a/b"),                         # mixed
    (BS + BS + "srv" + BS + "share", "//srv/share"),       # UNC
    ("~" + BS + "MEGA" + BS + "v", "~/MEGA/v"),
    ("~/MEGA/v", "~/MEGA/v"), ("/data/a", "/data/a"),      # already canonical: a fixed point
    ("/p/a" + BS + "b", "/p/a" + BS + "b"),                # a POSIX name with a backslash is a name, not a separator
    ("rel" + BS + "x", "rel" + BS + "x"),
    ("", ""), (None, None),
])
def test_canonical_path_rewrites_only_unmistakably_windows_paths(path, expected):
    assert canonical_path(path) == expected
    assert canonical_path(canonical_path(path)) == canonical_path(path)


def test_the_stored_forms_are_canonical_whatever_local_paths_wrote():
    loc = Locations("/db", "/data", notes_vault="C:/v", notes_vault_written="E:" + BS + "MEGA" + BS + "vault",
                    health_dir_written="~/health")
    assert (loc.notes_vault_stored, loc.health_dir_stored) == ("E:/MEGA/vault", "~/health")


def test_a_database_records_the_platform_that_wrote_it(tmp_path):
    import sqlite3
    import build_info_store
    from build_port import InputState, Provenance, RepoState
    con = sqlite3.connect(":memory:")
    from schema_helper import full_schema
    con.executescript(full_schema())
    prov = Provenance(4, RepoState(None, None), RepoState(None, None), "3", "3", (), platform=sys.platform)
    build_info_store.BuildProvenance(Locations("/db", "/data"), str(tmp_path), None).write(con, prov, "t", "t")
    assert build_info_store.latest(con)["build"]["platform"] == sys.platform


def test_load_reads_from_the_redirected_home_and_stores_the_written_form(monkeypatch, tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "local_paths.py").write_text(
        'PRIVATE_DATA_DIR = "/data"\nKNOWLEDGE_DB_DIR = "~/MEGA/db"\nNOTES_VAULT = "~/MEGA/vault"\n'
        'HEALTH_DIR = "~/MEGA/health"\nCONCERTS_CSV = "~/MEGA/c.csv"\nRYM_EXPORT_CSV = "~/MEGA/r.csv"\n'
        'SCROBBLES_JSON = "~/MEGA/s.json"\n', encoding="utf-8")
    home = tmp_path / "drive"
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", str(private))
    monkeypatch.setenv(locations.HOME_ENV_VAR, str(home))
    loc = paths.load()
    root = canonical_path(str(home))
    # reads use the redirected root, in canonical spelling ...
    assert loc.knowledge_db_dir == root + "/MEGA/db" and loc.notes_vault == root + "/MEGA/vault"
    assert loc.concerts_csv == root + "/MEGA/c.csv" and loc.scrobbles_json == root + "/MEGA/s.json"
    # ... while what the database stores is the text local_paths.py wrote, so it carries no machine's root
    assert (loc.notes_vault_stored, loc.health_dir_stored) == ("~/MEGA/vault", "~/MEGA/health")
    assert (loc.concerts_csv_stored, loc.rym_export_csv_stored, loc.scrobbles_json_stored) == \
        ("~/MEGA/c.csv", "~/MEGA/r.csv", "~/MEGA/s.json")
    assert "\\" not in loc.notes_vault and "\\" not in loc.private_data_dir
