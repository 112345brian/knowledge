"""The notes vault and the health directory are optional, generically named settings, not core requirements.

A checkout with only a database directory and a private data directory must load and build the core. The
vault is `NOTES_VAULT` (the old `BODYBUILDING_VAULT` still works); a step that needs a missing path says which
setting to define instead of formatting `None` into a path."""
import client_bootstrap
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(tmp_path, lines, monkeypatch):
    import paths
    (tmp_path / "local_paths.py").write_text("KNOWLEDGE_DB_DIR = '/db'\nPRIVATE_DATA_DIR = '/data'\n" + lines)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", str(tmp_path))
    return paths.load()


def test_a_config_with_only_the_two_core_directories_loads(tmp_path, monkeypatch):
    loc = _load(tmp_path, "", monkeypatch)
    assert (loc.notes_vault, loc.health_dir) == (None, None)


def test_notes_vault_is_the_generic_name(tmp_path, monkeypatch):
    assert _load(tmp_path, "NOTES_VAULT = '/vault'\n", monkeypatch).notes_vault == "/vault"


def test_the_old_vault_name_still_works(tmp_path, monkeypatch):
    assert _load(tmp_path, "BODYBUILDING_VAULT = '/old'\n", monkeypatch).notes_vault == "/old"


def test_the_new_name_wins_when_both_are_set(tmp_path, monkeypatch):
    assert _load(tmp_path, "BODYBUILDING_VAULT = '/old'\nNOTES_VAULT = '/new'\n", monkeypatch).notes_vault == "/new"


def test_need_names_the_setting_to_define():
    from locations import Locations
    loc = Locations(knowledge_db_dir="/db", private_data_dir="/data")
    with pytest.raises(ValueError, match="NOTES_VAULT"):
        loc.need("notes_vault", "NOTES_VAULT", "the measurements source")
    assert Locations(knowledge_db_dir="/db", private_data_dir="/data", notes_vault="/v").need(
        "notes_vault", "NOTES_VAULT", "x") == "/v"


def test_origin_path_resolution_without_a_vault_finds_nothing_instead_of_crashing():
    from ingest import fact_ingest_rules as r
    assert r.resolve_origin_path("Some Title, with a comma.md", None) is None
    assert r.resolve_origin_path("note.md", None) is None
    assert r.resolve_origin_path("note.md", "/v") == "/v/note.md"


def test_a_placeholder_with_no_setting_names_the_setting_to_define():
    from ingest import source_ingest_rules as r
    with pytest.raises(ValueError, match="NOTES_VAULT"):
        r.format_origin_path("{VAULT}/a.md", None, "/h")
    with pytest.raises(ValueError, match="HEALTH_DIR"):
        r.format_origin_path("{HEALTH}/a.csv", "/v", None)
    assert r.format_origin_path("/abs/a.md", None, None) == "/abs/a.md"
    assert r.format_origin_path("{VAULT}/a.md", "/v", None) == "/v/a.md"


def test_the_vault_input_is_listed_only_when_a_vault_is_configured():
    import build_info_store
    from locations import Locations
    without = build_info_store.input_paths(Locations(knowledge_db_dir="/db", private_data_dir="/data"), client_bootstrap.registry())
    assert not [k for k, _ in without if k.endswith("vault-db")]
    with_vault = build_info_store.input_paths(Locations(knowledge_db_dir="/db", private_data_dir="/data", notes_vault="/v"), client_bootstrap.registry())
    assert ("input:vault-db", "/v/bodybuilding.db") in with_vault      # a POSIX fake joins as POSIX on every host


def test_no_vault_means_no_citation_notes():
    import local_inputs
    import local_stores
    from ingest import source_readers
    from locations import Locations
    from pipeline_context import StepContext
    ctx = StepContext(Locations(knowledge_db_dir="/db", private_data_dir="/data"), local_inputs, local_stores)
    assert source_readers.VaultNotes(ctx.inputs, ctx.locations).records() == []


def test_the_core_does_not_name_the_bodybuilding_vault():
    """Only paths.py may mention the old setting name (as the accepted legacy alias)."""
    offenders = []
    for folder in (REPO, os.path.join(REPO, "ingest")):
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py") and name != "paths.py":
                text = open(os.path.join(folder, name), encoding="utf-8").read()
                if re.search(r"bodybuilding_vault|BODYBUILDING_VAULT", text):
                    offenders.append(os.path.relpath(os.path.join(folder, name), REPO))
    assert offenders == []


def test_the_vault_and_health_dir_keep_their_as_written_form_beside_the_expanded_one(tmp_path, monkeypatch):
    loc = _load(tmp_path, "NOTES_VAULT = '~/vault'\nHEALTH_DIR = '~/health'\n", monkeypatch)
    assert loc.notes_vault.endswith("vault") and not loc.notes_vault.startswith("~")
    assert (loc.notes_vault_stored, loc.health_dir_stored) == ("~/vault", "~/health")


def test_a_hand_built_locations_stores_its_paths_as_given():
    from locations import Locations
    loc = Locations(knowledge_db_dir="/db", private_data_dir="/data", notes_vault="/v", health_dir="/h")
    assert (loc.notes_vault_stored, loc.health_dir_stored) == ("/v", "/h")
    assert Locations(knowledge_db_dir="/db", private_data_dir="/data").notes_vault_stored is None
