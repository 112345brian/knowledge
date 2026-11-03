"""Deterministic export and import of the curated database (increment 2b of docs/db-source-of-truth.md).

Fixture databases are built from the real schema.sql in a temp dir; nothing here touches a real database.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

import export_rules as rules
import client_bootstrap
from schema_helper import full_schema
from appctx import app
from config_files import write_config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE = os.path.join(REPO, "knowledge.py")


def make_db(path, extra_statement="Plain."):
    con = sqlite3.connect(path)
    con.executescript(full_schema())
    con.executescript("""
        INSERT INTO artists (id, name) VALUES (1, 'Some Band'), (2, 'Other Act');
        INSERT INTO metrics (id, key, label, unit) VALUES (1, 'weight', 'Weight', 'kg');
        INSERT INTO subjects (id, name, domain) VALUES (1, 'alpha', 'general'), (2, 'beta', 'general');
        INSERT INTO subject_aliases (subject_id, alias) VALUES (1, 'a');
        INSERT INTO vault_files (id, path) VALUES (1, 'notes/x.md');
        INSERT INTO sources (id, citekey, name, source_type) VALUES (1, 'smith2020', 'Smith 2020', 'primary');
        INSERT INTO entities (id, entity_key, canonical_name, name_norm, type) VALUES (1, 'acme', 'Acme', 'acme', 'organization');
        INSERT INTO claims (id, statement) VALUES (1, 'A claim');
    """)
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, source_key, origin_file_id, notes) "
                "VALUES (1, 1, ?, 'high', 'unreviewed', 'k1', 1, ?)", (extra_statement, "line one\nline two ☃ \"quoted\""))
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, source_key, status, superseded_by_fact_id) "
                "VALUES (2, 2, 'Older.', 'low', 'unreviewed', 'k2', 'superseded', 1)")
    con.execute("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (1, 1, 'p. 1', 'q')")
    con.execute("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, "
                "status, visibility) VALUES (1, 'k1', 1, '2026-01-01T00:00:00+00:00', 'cli', ?, 'high', 'active', 'private')",
                (extra_statement,))
    con.execute("INSERT INTO claim_facts (claim_id, fact_id) VALUES (1, 1)")
    con.execute("INSERT INTO measurements (id, subject_id, metric_id, value, measured_at, source_id, trust_level) "
                "VALUES (1, 1, 1, 71.5, '2026-01-02', 1, 'high')")
    con.commit()
    con.close()
    return path


class World:
    def __init__(self, root):
        self.root = str(root)
        self.db = make_db(os.path.join(self.root, "live.db"))
        self.dir = os.path.join(self.root, "export")
        self.new_db = os.path.join(self.root, "restored.db")
        self.notes = os.path.join(self.root, "vault-sources")
        os.makedirs(self.notes)
        open(os.path.join(self.notes, "smith2020.md"), "w", encoding="utf-8").write("---\ntitle: Smith\n---\nBody \u2603\n")
        open(os.path.join(self.notes, "README.md"), "w").write("readme")

    def container(self):
        from locations import Locations
        return app(Locations(knowledge_db_dir=self.root, private_data_dir=os.path.join(self.root, "data")))

    def api(self):
        import db_export
        return db_export.compose_export(self.db, self.dir, self.notes)

    def export(self, tables=(), groups=(), skip=(), notes=False):
        return self.api().export_database(tables, groups, skip, notes)

    def verify(self, tables=(), groups=(), skip=(), notes=False):
        return self.api().verify_export(tables, groups, skip, notes)

    def restore(self, with_bulk=True, notes_into=None):
        import db_export
        return db_export.compose_import(self.new_db, self.dir, notes_into).import_database(with_bulk, notes_into is not None)

    def files(self, directory=None):
        directory = directory or self.dir
        found = {}
        for base, _, names in os.walk(directory):
            for n in names:
                path = os.path.join(base, n)
                found[os.path.relpath(path, directory)] = open(path, "rb").read()
        return dict(sorted(found.items()))


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_an_export_has_core_and_bulk_parts_each_with_a_manifest(w):
    result = w.export()
    core = {rules.file_name("core", t) for t in rules.CORE_TABLES}
    bulk = {rules.file_name("bulk", t) for t in client_bootstrap.registry().data_tables()}
    assert set(w.files()) == core | bulk | {"core/manifest.json", "bulk/manifest.json"}
    assert result["core"]["facts"] == 2 and result["core"]["fact_revisions"] == 1 and result["core"]["publishers"] == 0
    assert result["bulk"]["artists"] == 2 and result["bulk"]["measurements"] == 1
    manifest = json.loads(w.files()["core/manifest.json"])
    assert manifest["tables"]["facts"]["rows"] == 2 and manifest["format"] == rules.FORMAT


def test_every_table_of_the_full_schema_is_exported_or_deliberately_skipped():
    """The guard behind "the folder is the whole backup": a table added to schema.sql or a client fragment that nobody
    lists in CORE_TABLES or a source's report_tables would silently be left out of every backup."""
    con = sqlite3.connect(":memory:")
    con.executescript(full_schema())
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    fts_shadow = {t for t in tables if any(t.startswith(v + "_") for v in ("facts_fts", "sources_fts", "authors_fts"))}
    fts = {"facts_fts", "sources_fts", "authors_fts"}
    exported = set(rules.CORE_TABLES) | set(client_bootstrap.registry().data_tables())
    assert tables - exported - fts - fts_shadow - set(rules.NOT_EXPORTED) - {"sqlite_sequence"} == set()
    assert exported <= tables


def test_the_same_database_exports_to_the_same_bytes(w, tmp_path):
    w.export()
    first = w.files()
    w.dir = str(tmp_path / "second")
    w.export()
    assert w.files() == first


def test_rows_are_sorted_by_key_and_each_line_is_canonical_json(w):
    w.export()
    lines = w.files()["core/facts.ndjson"].decode().split("\n")[:-1]
    assert [json.loads(l)["id"] for l in lines] == [1, 2]
    assert all(l == json.dumps(json.loads(l), sort_keys=True, ensure_ascii=False, separators=(",", ":")) for l in lines)


def test_export_then_import_then_export_is_byte_identical_including_the_bulk_part(w, tmp_path):
    w.export()
    original = w.files()
    result = w.restore()
    assert result["core"]["facts"] == 2 and result["bulk"]["artists"] == 2
    second = str(tmp_path / "again")
    w.container().db_export(w.new_db, second).export_database()
    assert w.files(second) == original


def test_an_imported_database_is_a_working_knowledge_database(w):
    w.export()
    w.restore()
    con = sqlite3.connect(w.new_db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == sqlite3.connect(w.db).execute("PRAGMA user_version").fetchone()[0]
    assert con.execute("SELECT COUNT(*) FROM facts_fts WHERE facts_fts MATCH 'older'").fetchone()[0] == 1   # the full-text index
    assert con.execute("SELECT superseded_by_fact_id FROM facts WHERE id = 2").fetchone()[0] == 1
    assert con.execute("SELECT notes FROM facts WHERE id = 1").fetchone()[0] == "line one\nline two \u2603 \"quoted\""
    assert con.execute("SELECT name FROM artists ORDER BY id").fetchall() == [("Some Band",), ("Other Act",)]
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_core_only_import_creates_a_database_without_the_client_tables(w):
    w.export()
    result = w.restore(with_bulk=False)
    assert set(result) == {"core"}
    names = {r[0] for r in sqlite3.connect(w.new_db).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "facts" in names and "artists" not in names


def test_a_core_only_export_has_no_bulk_part_and_importing_it_still_works(w):
    w.export(tables=rules.CORE_TABLES)
    assert not any(n.startswith("bulk/") for n in w.files())
    assert set(w.restore()) == {"core"}                 # no bulk part: nothing to import, not an error


def test_verify_is_clean_after_an_export_and_names_the_drift_after_a_change(w):
    w.export()
    assert w.verify() == []
    con = sqlite3.connect(w.db)
    con.execute("UPDATE facts SET statement = 'Changed.' WHERE id = 1")
    con.execute("UPDATE artists SET name = 'Renamed' WHERE id = 1")
    con.commit()
    con.close()
    assert w.verify() == ["core/facts: the database has changed since the export",
                          "bulk/artists: the database has changed since the export"]
    assert w.verify() == w.verify()     # verifying writes nothing


def test_verify_reports_a_missing_part_and_an_edited_or_deleted_file(w):
    assert w.verify() == ["core/manifest.json is missing: this is not a complete export",
                          "bulk/manifest.json is missing: this is not a complete export"]
    w.export()
    path = os.path.join(w.dir, "core", "facts.ndjson")
    open(path, "a").write("\n")
    assert any(p.startswith("core/facts: differs from the manifest") for p in w.verify())
    os.remove(path)
    assert "core/facts: file is missing" in w.verify()


def test_verify_notices_a_client_table_that_appeared_after_the_export(w):
    w.export()
    os.remove(os.path.join(w.dir, "bulk", "artists.ndjson"))
    manifest_path = os.path.join(w.dir, "bulk", "manifest.json")
    manifest = json.load(open(manifest_path))
    del manifest["tables"]["artists"]
    json.dump(manifest, open(manifest_path, "w"))
    assert "bulk/artists: is in the database but not in the export" in w.verify()


def test_import_never_overwrites_a_database(w):
    w.export()
    open(w.new_db, "w").write("precious")
    with pytest.raises(FileExistsError):
        w.restore()
    assert open(w.new_db).read() == "precious"


@pytest.mark.parametrize("part,damage", [(p, d) for p in ("core", "bulk") for d in ("edit", "delete", "no-manifest", "truncate")])
def test_a_damaged_part_creates_no_database(w, part, damage):
    w.export()
    target = os.path.join(w.dir, part, "facts.ndjson" if part == "core" else "artists.ndjson")
    if damage == "edit":
        text = open(target).read().replace("Plain.", "Forged.").replace("Some Band", "Forged Band")
        open(target, "w").write(text)
    elif damage == "delete":
        os.remove(target)
    elif damage == "no-manifest":
        os.remove(os.path.join(w.dir, part, "manifest.json"))
    else:
        open(target, "wb").write(open(target, "rb").read()[:-5])
    if part == "bulk" and damage == "no-manifest":
        assert set(w.restore()) == {"core"}              # an absent bulk part is allowed; a damaged one is not
        return
    with pytest.raises(rules.ExportError):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_an_export_from_another_schema_version_is_refused_and_leaves_nothing(w):
    w.export()
    path = os.path.join(w.dir, "core", "manifest.json")
    manifest = json.load(open(path))
    manifest["schema_user_version"] += 1
    json.dump(manifest, open(path, "w"))
    with pytest.raises(rules.ExportError, match="schema version"):
        w.restore()
    assert not os.path.exists(w.new_db)


def _retype(w, part, table, mutate):
    """Rewrite a table file and its manifest so the export is internally consistent but its content is bad."""
    path = os.path.join(w.dir, part, rules.file_name(part, table).split("/")[1])
    rows = rules.decode_table(open(path).read())
    mutate(rows)
    text = rules.encode_table(rows)
    open(path, "w").write(text)
    manifest_path = os.path.join(w.dir, part, "manifest.json")
    manifest = json.load(open(manifest_path))
    manifest["tables"][table] = {"rows": text.count("\n"), "sha256": rules.digest(text)}
    json.dump(manifest, open(manifest_path, "w"))


def test_a_dangling_foreign_key_removes_the_new_database(w):
    w.export()
    _retype(w, "core", "fact_sources", lambda rows: rows[0].update(source_id=99))
    with pytest.raises(rules.ExportError, match="not sound"):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_a_column_this_schema_lacks_removes_the_new_database(w):
    w.export()
    _retype(w, "core", "subjects", lambda rows: rows[0].update(flavour="x"))
    with pytest.raises(rules.ExportError, match="lacks"):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_a_constraint_violation_in_a_bulk_table_removes_the_new_database(w):
    w.export()
    _retype(w, "bulk", "measurements", lambda rows: rows[0].update(trust_level="certain"))
    with pytest.raises(sqlite3.IntegrityError):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_a_bulk_manifest_naming_a_table_no_client_owns_or_a_path_is_refused(w):
    w.export()
    path = os.path.join(w.dir, "bulk", "manifest.json")
    for bad in ("sqlite_master", "../core/facts", "facts; DROP TABLE facts", "Facts"):
        manifest = json.load(open(path))
        manifest["tables"][bad] = {"rows": 0, "sha256": rules.digest("")}
        json.dump(manifest, open(path, "w"))
        with pytest.raises(rules.ExportError):
            w.restore()
        assert not os.path.exists(w.new_db)
        w.export()


def test_values_the_export_cannot_carry_are_refused():
    for bad in (b"blob", float("nan"), float("inf"), True, [1], {"a": 1}):
        with pytest.raises(rules.ExportError):
            rules.encode_row({"c": bad})
    assert rules.encode_row({"b": None, "a": 1.5, "c": "\u00e9"}) == '{"a":1.5,"b":null,"c":"\u00e9"}'


def test_decoding_refuses_garbage():
    with pytest.raises(rules.ExportError):
        rules.decode_table('{"a":1}')                  # no trailing newline
    with pytest.raises(rules.ExportError):
        rules.decode_table("not json\n")
    with pytest.raises(rules.ExportError):
        rules.decode_table("[1]\n")
    assert rules.decode_table("") == []


def test_exporting_a_database_that_lacks_a_core_table_or_does_not_exist_fails_cleanly(w, tmp_path):
    con = sqlite3.connect(w.db)
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("DROP TABLE claim_facts")
    con.commit()
    con.close()
    with pytest.raises(rules.ExportError, match="claim_facts"):
        w.export()
    assert not os.path.exists(w.dir)
    w.db = str(tmp_path / "missing.db")
    with pytest.raises(rules.ExportError, match="no database"):
        w.export()


def test_export_never_modifies_the_database(w):
    before = open(w.db, "rb").read()
    w.export(notes=True)
    w.verify(notes=True)
    assert open(w.db, "rb").read() == before


# ---------------------------------------------------------------- notes: opt-in, separate

def test_notes_are_not_exported_unless_asked_for(w):
    w.export()
    assert not any(n.startswith("notes/") for n in w.files())


def test_notes_part_copies_the_markdown_and_restores_without_overwriting(w, tmp_path):
    w.export(notes=True)
    files = w.files()
    assert files["notes/smith2020.md"].decode().endswith("Body \u2603\n") and "notes/README.md" in files
    assert w.verify(notes=True) == []
    target = str(tmp_path / "restored-notes")
    w.restore(notes_into=target)
    assert sorted(os.listdir(target)) == ["README.md", "smith2020.md"]
    assert open(os.path.join(target, "smith2020.md"), encoding="utf-8").read() == open(os.path.join(w.notes, "smith2020.md"), encoding="utf-8").read()
    os.remove(w.new_db)
    with pytest.raises(FileExistsError, match="never overwritten"):
        w.restore(notes_into=target)                 # the notes are already there
    assert not os.path.exists(w.new_db)               # and nothing was created before that was found out


def test_notes_drift_and_empty_or_missing_notes_are_reported(w):
    w.export(tables=rules.CORE_TABLES, notes=True)
    open(os.path.join(w.notes, "smith2020.md"), "a").write("edited")
    assert [p for p in w.verify(tables=("facts",), notes=True) if p.startswith("notes/")] == ["notes/smith2020.md: the database has changed since the export"]
    for name in os.listdir(w.notes):
        os.remove(os.path.join(w.notes, name))
    with pytest.raises(rules.ExportError, match="no notes"):
        w.export(tables=("facts",), notes=True)


def test_a_note_name_that_is_a_path_is_refused():
    for bad in ("../x.md", "a/b.md", "x.txt", ".md", "x..md", ""):
        with pytest.raises(rules.ExportError):
            rules.check_note(bad)


def test_unknown_names_and_empty_selections_are_refused(w):
    with pytest.raises(rules.ExportError, match="not an exportable table"):
        w.export(tables=("facts", "nonsense"))
    with pytest.raises(rules.ExportError, match="not a group"):
        w.export(groups=("everything",))
    with pytest.raises(rules.ExportError, match="not an exportable table"):
        w.export(skip=("nonsense",))
    with pytest.raises(rules.ExportError, match="nothing is selected"):
        w.export(tables=("facts",), skip=("facts",))
    assert not os.path.exists(w.dir)


# ---------------------------------------------------------------- granular selection

def test_a_single_table_can_be_exported_and_verified(w):
    result = w.export(tables=("artists",))
    assert result == {"bulk": {"artists": 2}}
    assert sorted(w.files()) == ["bulk/artists.ndjson", "bulk/manifest.json"]
    assert w.verify(tables=("artists",)) == []
    assert w.verify(tables=("facts",)) == ["core/manifest.json is missing: this is not a complete export"]


def test_groups_and_skip_pick_tables(w):
    assert set(w.export(groups=("facts",))["core"]) == {"facts", "fact_revisions", "fact_sources", "fact_entities"}
    assert set(w.export(groups=("facts",), skip=("fact_entities",))["core"]) == {"facts", "fact_revisions", "fact_sources"}
    music = w.export(groups=("concerts",))["bulk"]               # a client source's name is a group of its tables
    assert "artists" in music and "measurements" not in music
    assert set(w.export(groups=("subjects",), tables=("claims",))["core"]) == {"subjects", "subject_aliases", "claims"}
    everything_but = w.export(skip=("facts", "fact_revisions"))
    assert "facts" not in everything_but["core"] and "artists" in everything_but["bulk"]


def test_partial_exports_merge_into_what_the_folder_already_holds(w):
    w.export(tables=("facts",))
    w.export(tables=("artists",))
    w.export(tables=("claims",))
    core = json.loads(w.files()["core/manifest.json"])["tables"]
    assert sorted(core) == ["claims", "facts"] and sorted(json.loads(w.files()["bulk/manifest.json"])["tables"]) == ["artists"]
    assert w.verify(tables=("facts", "claims", "artists")) == []
    w.export()                                                    # a full export afterwards agrees with everything
    assert w.verify() == []
    assert len(json.loads(w.files()["core/manifest.json"])["tables"]) == len(rules.CORE_TABLES)


def test_a_partial_export_after_a_schema_change_drops_the_stale_entries(w):
    w.export()
    path = os.path.join(w.dir, "core", "manifest.json")
    manifest = json.load(open(path))
    manifest["schema_user_version"] -= 1
    json.dump(manifest, open(path, "w"))
    w.export(tables=("facts",))
    assert sorted(json.loads(w.files()["core/manifest.json"])["tables"]) == ["facts"]    # the old version's entries are gone


def test_an_unselected_table_that_drifted_is_not_reported(w):
    w.export()
    con = sqlite3.connect(w.db)
    con.execute("UPDATE artists SET name = 'Renamed' WHERE id = 1")
    con.commit()
    con.close()
    assert w.verify(tables=("facts",)) == []
    assert w.verify(groups=("concerts",)) == ["bulk/artists: the database has changed since the export"]


def test_a_partial_export_that_cannot_stand_alone_is_not_importable_and_leaves_nothing(w):
    w.export(tables=("facts",))                                   # facts point at subjects, which are not in the export
    with pytest.raises(rules.ExportError, match="not sound"):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_a_partial_export_that_stands_alone_imports(w):
    w.export(tables=("subjects", "subject_aliases"))
    result = w.restore()
    assert result == {"core": {"subjects": 2, "subject_aliases": 1}}
    assert sqlite3.connect(w.new_db).execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_an_export_folder_with_no_table_parts_cannot_be_imported(w, tmp_path):
    with pytest.raises(rules.ExportError, match="no table files"):
        w.restore()


# ---------------------------------------------------------------- the logs

def with_logs(w):
    import live_schema_store
    con = sqlite3.connect(w.db)
    live_schema_store.install(con)
    live_schema_store.set_context(con, "cli", "s-1", "2026-11-04T10:00:00Z")
    con.execute("UPDATE facts SET notes = 'changed' WHERE id = 2")
    con.execute("INSERT INTO access_log (accessed_at, source_key, kind, via, mode) VALUES ('t', 'k1', 'show', 'cli', 'private')")
    con.execute("INSERT INTO operation_log (started_at, name, via, outcome) VALUES ('t', 'x', 'cli', 'ok')")
    con.commit()
    con.close()


def test_logs_are_left_out_when_the_database_has_none_and_exported_when_it_has(w):
    assert not set(rules.LOG_TABLES) & set(w.export()["core"])
    with_logs(w)
    result = w.export()["core"]
    assert result["audit_log"] >= 1 and result["access_log"] == 1 and result["operation_log"] == 1


def test_logs_round_trip_and_the_restored_database_still_refuses_to_edit_them(w, tmp_path):
    with_logs(w)
    w.export()
    original = w.files()
    w.restore()
    con = sqlite3.connect(w.new_db)
    assert con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] >= 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM audit_log")
    second = str(tmp_path / "again")
    w.container().db_export(w.new_db, second).export_database()
    assert w.files(second) == original


def test_only_the_logs_can_be_exported_and_naming_them_on_a_database_without_them_fails(w):
    with pytest.raises(rules.ExportError, match="lacks table"):
        w.export(tables=("audit_log",))
    with pytest.raises(rules.ExportError, match="nothing is selected"):
        w.export(groups=("logs",))                              # a group whose members are all absent selects nothing
    with_logs(w)
    assert set(w.export(groups=("logs",))["core"]) == set(rules.LOG_TABLES)


# ---------------------------------------------------------------- the command line

def cli(w, *args):
    config = write_config(os.path.join(w.root, "config.toml"), w.root, os.path.join(w.root, "data"),
                          notes_vault=os.path.join(w.root, "vault"))
    return subprocess.run([sys.executable, KNOWLEDGE, "db", *args], capture_output=True, text=True, cwd=w.root,
                          env={**os.environ, "KNOWLEDGE_CONFIG": config})


def test_the_commands_export_verify_and_import(w):
    r = cli(w, "export", "--db", w.db, "--dir", w.dir)
    assert r.returncode == 0, r.stderr
    assert "core/facts: 2" in r.stdout and "bulk/artists: 2" in r.stdout
    assert cli(w, "verify", "--db", w.db, "--dir", w.dir).stdout.strip() == "the export matches the database"
    r = cli(w, "import", "--into", w.new_db, "--dir", w.dir)
    assert r.returncode == 0 and "core/facts: 2" in r.stdout and "bulk/artists: 2" in r.stdout, r.stderr
    r = cli(w, "import", "--into", w.new_db, "--dir", w.dir)
    assert r.returncode == 1 and "already exists" in r.stderr


def test_the_selection_and_notes_flags(w):
    r = cli(w, "export", "--db", w.db, "--dir", w.dir, "--table", "facts", "--table", "artists")
    assert r.returncode == 0 and "core/facts: 2" in r.stdout and "bulk/artists: 2" in r.stdout and "measurements" not in r.stdout, r.stderr
    r = cli(w, "export", "--db", w.db, "--dir", w.dir, "--group", "claims", "--skip", "claim_facts")
    assert r.returncode == 0 and "core/claims: 1" in r.stdout and "claim_facts" not in r.stdout
    r = cli(w, "export", "--db", w.db, "--dir", w.dir, "--table", "nonsense")
    assert r.returncode == 1 and "not an exportable table" in r.stderr
    assert cli(w, "verify", "--db", w.db, "--dir", w.dir, "--table", "facts").returncode == 0
    vault_sources = os.path.join(w.root, "vault", "sources")
    os.makedirs(vault_sources)
    open(os.path.join(vault_sources, "a.md"), "w").write("a")
    r = cli(w, "export", "--db", w.db, "--dir", w.dir, "--include-notes")
    assert r.returncode == 0 and "notes: 1 file(s)" in r.stdout, r.stderr
    notes_out = os.path.join(w.root, "notes-out")
    r = cli(w, "import", "--into", w.new_db, "--dir", w.dir, "--notes-into", notes_out, "--core-only")
    assert r.returncode == 0 and os.listdir(notes_out) == ["a.md"], r.stderr
    assert "bulk" not in r.stdout


def test_verify_exits_1_on_drift_and_the_default_directory_is_in_the_data_dir(w):
    assert cli(w, "export", "--db", w.db).returncode == 0
    assert os.path.isfile(os.path.join(w.root, "data", "export", "core", "manifest.json"))
    con = sqlite3.connect(w.db)
    con.execute("DELETE FROM claim_facts")
    con.commit()
    con.close()
    r = cli(w, "verify", "--db", w.db)
    assert r.returncode == 1 and "claim_facts" in r.stderr
