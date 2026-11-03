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
from appctx import app
from config_files import write_unused_inputs_config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE = os.path.join(REPO, "knowledge.py")


def make_db(path, extra_statement="Plain."):
    con = sqlite3.connect(path)
    with open(os.path.join(REPO, "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    con.executescript("""
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
    con.commit()
    con.close()
    return path


class World:
    def __init__(self, root):
        self.root = str(root)
        self.db = make_db(os.path.join(self.root, "live.db"))
        self.dir = os.path.join(self.root, "export")
        self.new_db = os.path.join(self.root, "restored.db")

    def container(self):
        from locations import Locations
        return app(Locations(knowledge_db_dir=self.root, private_data_dir=os.path.join(self.root, "data")))

    def export(self):
        return self.container().db_export(self.db, self.dir).export_database()

    def verify(self):
        return self.container().db_export(self.db, self.dir).verify_export()

    def restore(self):
        return self.container().db_import(self.new_db, self.dir).import_database()

    def files(self, directory=None):
        directory = directory or self.dir
        return {n: open(os.path.join(directory, n), "rb").read() for n in sorted(os.listdir(directory))}


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def test_an_export_has_one_file_per_table_and_a_manifest(w):
    counts = w.export()
    assert set(w.files()) == {rules.file_name(t) for t in rules.EXPORT_TABLES} | {"manifest.json"}
    assert counts["facts"] == 2 and counts["fact_revisions"] == 1 and counts["publishers"] == 0
    manifest = json.loads(w.files()["manifest.json"])
    assert manifest["tables"]["facts"]["rows"] == 2 and manifest["format"] == rules.FORMAT


def test_the_same_database_exports_to_the_same_bytes(w, tmp_path):
    w.export()
    first = w.files()
    w.dir = str(tmp_path / "second")
    w.export()
    assert w.files() == first


def test_rows_are_sorted_by_key_and_each_line_is_canonical_json(w):
    w.export()
    lines = w.files()["facts.ndjson"].decode().split("\n")[:-1]
    assert [json.loads(l)["id"] for l in lines] == [1, 2]
    assert all(l == json.dumps(json.loads(l), sort_keys=True, ensure_ascii=False, separators=(",", ":")) for l in lines)


def test_export_then_import_then_export_is_byte_identical(w, tmp_path):
    w.export()
    original = w.files()
    assert w.restore()["facts"] == 2
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
    assert con.execute("SELECT notes FROM facts WHERE id = 1").fetchone()[0] == "line one\nline two ☃ \"quoted\""
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_verify_is_clean_after_an_export_and_names_the_drift_after_a_change(w):
    w.export()
    assert w.verify() == []
    con = sqlite3.connect(w.db)
    con.execute("UPDATE facts SET statement = 'Changed.' WHERE id = 1")
    con.commit()
    con.close()
    assert w.verify() == ["facts: the database has changed since the export"]
    assert w.verify() == w.verify()     # verifying writes nothing


def test_verify_reports_a_missing_manifest_and_an_edited_file(w):
    assert w.verify() == ["manifest.json is missing: this is not a complete export"]
    w.export()
    path = os.path.join(w.dir, "facts.ndjson")
    open(path, "a").write("\n")
    assert any(p.startswith("facts: differs from the manifest") for p in w.verify())
    os.remove(path)
    assert "facts: file is missing" in w.verify()


def test_import_never_overwrites_a_database(w):
    w.export()
    open(w.new_db, "w").write("precious")
    with pytest.raises(FileExistsError):
        w.restore()
    assert open(w.new_db).read() == "precious"


@pytest.mark.parametrize("damage", ["edit", "delete", "no-manifest", "truncate"])
def test_a_damaged_export_creates_no_database(w, damage):
    w.export()
    facts = os.path.join(w.dir, "facts.ndjson")
    if damage == "edit":
        text = open(facts).read().replace("Plain.", "Forged.")
        open(facts, "w").write(text)
    elif damage == "delete":
        os.remove(facts)
    elif damage == "no-manifest":
        os.remove(os.path.join(w.dir, "manifest.json"))
    else:
        open(facts, "wb").write(open(facts, "rb").read()[:-5])
    with pytest.raises(rules.ExportError):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_an_export_from_another_schema_version_is_refused_and_leaves_nothing(w):
    w.export()
    path = os.path.join(w.dir, "manifest.json")
    manifest = json.load(open(path))
    manifest["schema_user_version"] += 1
    json.dump(manifest, open(path, "w"))
    with pytest.raises(rules.ExportError, match="schema version"):
        w.restore()
    assert not os.path.exists(w.new_db)


def _retype(w, table, mutate):
    """Rewrite a table file and the manifest so the export is internally consistent but its content is bad."""
    path = os.path.join(w.dir, rules.file_name(table))
    rows = rules.decode_table(open(path).read())
    mutate(rows)
    text = rules.encode_table(rows)
    open(path, "w").write(text)
    manifest = json.load(open(os.path.join(w.dir, "manifest.json")))
    manifest["tables"][table] = {"rows": text.count("\n"), "sha256": rules.digest(text)}
    json.dump(manifest, open(os.path.join(w.dir, "manifest.json"), "w"))


def test_a_dangling_foreign_key_removes_the_new_database(w):
    w.export()
    _retype(w, "fact_sources", lambda rows: rows[0].update(source_id=99))
    with pytest.raises(rules.ExportError, match="not sound"):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_a_column_this_schema_lacks_removes_the_new_database(w):
    w.export()
    _retype(w, "subjects", lambda rows: rows[0].update(flavour="x"))
    with pytest.raises(rules.ExportError, match="lacks"):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_a_constraint_violation_removes_the_new_database(w):
    w.export()
    _retype(w, "facts", lambda rows: rows[0].update(trust_level="certain"))
    with pytest.raises(sqlite3.IntegrityError):
        w.restore()
    assert not os.path.exists(w.new_db)


def test_values_the_export_cannot_carry_are_refused():
    for bad in (b"blob", float("nan"), float("inf"), True, [1], {"a": 1}):
        with pytest.raises(rules.ExportError):
            rules.encode_row({"c": bad})
    assert rules.encode_row({"b": None, "a": 1.5, "c": "é"}) == '{"a":1.5,"b":null,"c":"é"}'


def test_decoding_refuses_garbage():
    with pytest.raises(rules.ExportError):
        rules.decode_table('{"a":1}')                  # no trailing newline
    with pytest.raises(rules.ExportError):
        rules.decode_table("not json\n")
    with pytest.raises(rules.ExportError):
        rules.decode_table("[1]\n")
    assert rules.decode_table("") == []


def test_exporting_a_database_that_lacks_a_table_or_does_not_exist_fails_cleanly(w, tmp_path):
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
    w.export()
    w.verify()
    assert open(w.db, "rb").read() == before


# ---------------------------------------------------------------- the command line

def cli(w, *args):
    config = write_unused_inputs_config(os.path.join(w.root, "config.toml"), w.root, os.path.join(w.root, "data"),
                                        os.path.join(w.root, "unused"))
    return subprocess.run([sys.executable, KNOWLEDGE, "db", *args], capture_output=True, text=True, cwd=w.root,
                          env={**os.environ, "KNOWLEDGE_CONFIG": config})


def test_the_commands_export_verify_and_import(w):
    r = cli(w, "export", "--db", w.db, "--dir", w.dir)
    assert r.returncode == 0, r.stderr
    assert "facts: 2" in r.stdout
    assert cli(w, "verify", "--db", w.db, "--dir", w.dir).stdout.strip() == "the export matches the database"
    r = cli(w, "import", "--into", w.new_db, "--dir", w.dir)
    assert r.returncode == 0 and "facts: 2" in r.stdout, r.stderr
    r = cli(w, "import", "--into", w.new_db, "--dir", w.dir)
    assert r.returncode == 1 and "already exists" in r.stderr


def test_verify_exits_1_on_drift_and_the_default_directory_is_in_the_data_dir(w):
    assert cli(w, "export", "--db", w.db).returncode == 0
    assert os.path.isfile(os.path.join(w.root, "data", "export", "manifest.json"))
    con = sqlite3.connect(w.db)
    con.execute("DELETE FROM claim_facts")
    con.commit()
    con.close()
    r = cli(w, "verify", "--db", w.db)
    assert r.returncode == 1 and "claim_facts" in r.stderr
