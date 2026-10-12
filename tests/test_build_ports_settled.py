"""The normal-db and build ports against the foundation (docs: the PR that settled them).

Three things are pinned here so they cannot drift back:

* `step_stores_port.StepStores` is the transitional adapter of the legacy `run(con, ctx)` steps. Every method must
  have a named user; the map below is the list of who still needs it, so a method nobody calls fails the test and a
  user that stops calling it makes its entry stale (delete the entry and, when it was the last, the method).
* `build_files_port.BuildFiles` and its adapter define each operation once.
* A path recorded by the provenance reader (`build_inputs`) is a key, never a path, and the vault the reader
  looks in is the expanded one while what the source-notes manifest hashes is independent of the stored form.
"""
import ast
import hashlib
import inspect
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path[:0] = [REPO, HERE]

import build_files_port  # noqa: E402
import build_info_store as bi  # noqa: E402
import local_buildfiles  # noqa: E402
import local_stores  # noqa: E402
import private_git  # noqa: E402
import step_stores_port  # noqa: E402
from locations import Locations  # noqa: E402
from test_build_info import new_db  # noqa: E402

# method -> the production files that call it. A file is named only when it really reads `.<method>`.
STEP_STORES_USERS = {
    "load_privacy_rules": ("fact_ingest_service.py",),          # via fact_ingest.compose(privacy_floor=stores)
    "apply_privacy_rules": ("fact_ingest_service.py",),         # same
    "read_entities": ("ingest/link_entities.py",),
    "read_subjects": ("ingest/seed_subject_hierarchy.py",),
    "subject_repository": ("fact_ingest.py", "ingest/seed_subject_hierarchy.py"),
    "insert_claim": ("client/seed_claims.py",),
    "link_claim_premise": ("client/seed_claims.py",),
    "source_repository": ("fact_ingest.py", "ingest/link_source_relations.py", "ingest/literature_sources.py",
                          "ingest/seed_sources.py"),
    "fact_repository": ("fact_ingest.py",),
    "read_log": ("revision_apply_service.py",),                 # via ingest/apply_fact_revisions.py(revision_log=stores)
    "load_rules": ("revision_apply_service.py",),               # same (rules_reader=stores)
    "fact_revision_table": ("ingest/apply_fact_revisions.py",),
    "fact_privacy_table": ("ingest/apply_fact_revisions.py",),
    "entity_link_table": ("ingest/link_entities.py",),
}


def _protocol_methods(proto):
    return [n.name for n in ast.parse(inspect.getsource(proto)).body[0].body if isinstance(n, ast.FunctionDef)]


def _source(rel):
    with open(os.path.join(REPO, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def test_every_step_stores_method_has_a_named_user_and_the_user_still_calls_it():
    methods = _protocol_methods(step_stores_port.StepStores)
    assert sorted(methods) == sorted(STEP_STORES_USERS), \
        "StepStores changed: every method needs an entry (who calls it) in STEP_STORES_USERS, and no entry may be left over"
    for method, users in sorted(STEP_STORES_USERS.items()):
        for rel in users:
            attr_calls = [n for n in ast.walk(ast.parse(_source(rel)))
                          if isinstance(n, ast.Attribute) and n.attr == method]
            assert attr_calls, f"{rel} no longer calls .{method}: drop it from STEP_STORES_USERS (and the method when none is left)"


def test_the_adapter_has_exactly_the_methods_of_the_port():
    port = set(_protocol_methods(step_stores_port.StepStores))
    own = {name for name, fn in vars(local_stores).items()
           if inspect.isfunction(fn) and fn.__module__ == "local_stores" and not name.startswith("_")}
    assert own == port


def _defined_names(tree_body):
    return [n.name for n in tree_body if isinstance(n, ast.FunctionDef)]


def test_build_files_defines_each_operation_once():
    proto = ast.parse(inspect.getsource(build_files_port.BuildFiles)).body[0].body
    adapter = ast.parse(inspect.getsource(local_buildfiles)).body
    for label, names in (("BuildFiles", _defined_names(proto)), ("local_buildfiles", _defined_names(adapter))):
        assert len(names) == len(set(names)), f"{label} defines twice: " + ", ".join(sorted({n for n in names if names.count(n) > 1}))
    assert set(_defined_names(proto)) == set(_defined_names(adapter))


# ------------------------------------------------------------------ provenance and stored paths

def _locations(tmp_path):
    root = tmp_path / "in"
    (root / "vault").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "vault" / "bodybuilding.db").write_bytes(b"vault-db-bytes")
    return Locations(knowledge_db_dir=str(root / "db"), private_data_dir=str(root / "data"),
                     notes_vault=str(root / "vault"), notes_vault_written="~/MEGA/vault",
                     health_dir=str(root / "health"), health_dir_written="~/MEGA/health")


def test_the_vault_input_is_read_from_the_expanded_directory_and_no_path_is_recorded(tmp_path):
    loc = _locations(tmp_path)
    paths = dict(bi.input_paths(loc))
    assert paths["input:vault-db"] == os.path.join(loc.notes_vault, "bodybuilding.db")   # read where the file is
    assert "~" not in paths["input:vault-db"]
    con = new_db()
    reader = bi.BuildProvenance(loc, str(tmp_path), private_git)
    reader.write(con, reader.read(con), "2026-10-03T08:00:00+00:00", "2026-10-03T08:00:01+00:00")
    rows = list(con.execute("SELECT * FROM build_inputs"))
    assert {r["input_key"]: r["state"] for r in rows}["input:vault-db"] == "present"
    for cell in (c for r in rows for c in tuple(r)):
        assert not any(p and str(p) in str(cell) for p in (loc.notes_vault, loc.notes_vault_written, str(tmp_path), "MEGA")), cell


def _manifest_with(origin_path):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE sources (origin_path TEXT, content_sha256 TEXT, size_bytes INTEGER, file_mtime TEXT)")
    con.execute("INSERT INTO sources VALUES (?, 'abc', 5, '2026-01-01T00:00:00')", (origin_path,))
    return bi._notes_manifest(con)


def test_the_source_notes_manifest_does_not_depend_on_the_stored_vault_form():
    stored = _manifest_with("~/MEGA/vault/note.md")
    expanded = _manifest_with("C:/Users/x/MEGA/vault/note.md")
    assert stored == expanded and stored["state"] == "present"
    assert stored["sha256"] == hashlib.sha256(b"note.md:abc").hexdigest()
