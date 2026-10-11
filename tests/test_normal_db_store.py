"""The SQLite adapters (`normal_db_store`, `leak_scan_store`, `sql_runner_store`) against real files: what they
read and write, with the policy left to the domain. Rows cross the ports as plain data."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import leak_scan_store  # noqa: E402
import leak_test  # noqa: E402
import local_buildfiles  # noqa: E402
import normal_db_store  # noqa: E402
import sql_runner_store  # noqa: E402
from normal_rules import FullDbRows, Integrity, NormalDbError, TABLES, TableRows, select_content, verify_integrity  # noqa: E402
import privacy  # noqa: E402

M = leak_test.MARKERS


@pytest.fixture
def full(tmp_path):
    return leak_test.build_fixture(local_buildfiles, str(tmp_path))


def test_read_rows_returns_plain_rows_of_the_normal_visibility_only(full):
    data = normal_db_store.read_rows(full)
    assert isinstance(data, FullDbRows)
    assert [r[2] for r in data.facts if r[12] != "normal"] == []
    assert M["private fact text"] not in {r[2] for r in data.facts}
    assert {r[1] for r in data.subjects} >= {"nutrition", "sleep", M["private subject name"]}   # all subjects: the floor is policy
    assert all(e[1] != "quillon-fernsby" for e in data.entities)                                # a private entity is never read
    assert data.build_info[1] == "2026-10-08T00:00:00+00:00"
    assert all(isinstance(r, tuple) for r in data.facts)                                         # no cursor or row object


def test_read_rows_never_writes_the_full_db(full):
    before = open(full, "rb").read()
    normal_db_store.read_rows(full)
    assert open(full, "rb").read() == before
    assert not os.path.exists(full + "-journal")


def test_read_rows_does_not_create_a_missing_database(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        normal_db_store.read_rows(str(tmp_path / "nope.db"))
    assert not (tmp_path / "nope.db").exists()


def test_write_then_inspect_round_trips_and_rebuilds_the_text_index(tmp_path, full):
    out = str(tmp_path / "n.db")
    normal_db_store.write(out, select_content(normal_db_store.read_rows(full), privacy.Rules()))
    integrity = normal_db_store.inspect(out)
    assert isinstance(integrity, Integrity) and set(integrity.counts) == set(TABLES)
    assert integrity.foreign_key_violations == [] and integrity.trigger_count == 0
    verify_integrity(integrity)
    con = sqlite3.connect(out)
    assert con.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'creatine'").fetchall()


def test_write_enforces_the_schema_checks(tmp_path):
    bad = [TableRows("facts", ("id", "subject_id", "statement", "is_original_claim", "trust_level", "status", "date_added",
                               "visibility", "freshness", "kind"),
                     [(1, 1, "s", 0, "medium", "active", "2026-01-01", "private", "unreviewed", "observation")])]
    with pytest.raises(sqlite3.IntegrityError):               # visibility = 'normal' is a CHECK, not just a filter
        normal_db_store.write(str(tmp_path / "n.db"), bad)


def test_inspect_reports_a_dangling_reference_and_an_extra_object(tmp_path, full):
    out = str(tmp_path / "n.db")
    normal_db_store.write(out, select_content(normal_db_store.read_rows(full), privacy.Rules()))
    con = sqlite3.connect(out)
    con.execute("INSERT INTO fact_sources (fact_id, source_id) VALUES (999, 999)")
    con.execute("CREATE TABLE claims (id INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(NormalDbError):
        verify_integrity(normal_db_store.inspect(out))
    assert normal_db_store.inspect(out).foreign_key_violations


def test_cells_name_table_and_column_and_include_every_row(tmp_path, full):
    out = str(tmp_path / "n.db")
    normal_db_store.write(out, select_content(normal_db_store.read_rows(full), privacy.Rules()))
    cells = list(leak_scan_store.cells(out))
    assert ("facts.statement", leak_test.NORMAL_STATEMENTS[0]) in cells
    assert ("subjects.name", "nutrition") in cells


def test_private_material_carries_every_private_category_of_the_fixture(full):
    m = leak_scan_store.private_material(full)
    assert (3, M["private fact text"]) == m.private_facts[0][:2] or any(r[1] == M["private fact text"] for r in m.private_facts)
    assert (M["claim text"] in {r[1] for r in m.claims}) and ((M["vault path"],) in m.vault_paths)
    assert (M["private subject name"],) in m.private_subject_names
    assert any(r[0] == M["private entity name"] for r in m.private_entities)
    assert (M["private entity alias"], M["private entity alias"].casefold()) in m.private_entity_aliases
    assert (M["code commit"], M["private commit"]) in m.build_commits
    assert (M["file hash"],) in m.file_hashes and (M["where_from URL"],) in m.where_from
    assert "Creatine monohydrate is well studied for strength." in m.legit


def test_sql_runner_runs_scripts_then_statements_with_foreign_keys_on(tmp_path):
    path = str(tmp_path / "r.db")
    sql_runner_store.run(path, ["CREATE TABLE a (id INTEGER PRIMARY KEY); CREATE TABLE b (a_id INTEGER REFERENCES a(id));"],
                         [("INSERT INTO a (id) VALUES (?)", (1,)), ("INSERT INTO b (a_id) VALUES (?)", (1,))])
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM b").fetchone() == (1,)
    with pytest.raises(sqlite3.IntegrityError):
        sql_runner_store.run(path, (), [("INSERT INTO b (a_id) VALUES (?)", (42,))])
    assert con.execute("SELECT COUNT(*) FROM b").fetchone() == (1,)    # the failed run committed nothing
