"""The live database's audit and append-only triggers (increment 2 of docs/db-source-of-truth.md).

Fixture databases are built from the real schema.sql in memory; nothing here touches a real database.
"""
import os
import sqlite3

import pytest

import audit_rules
import live_schema_store as live
import normal_rules
import privacy_store

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_TABLES = {"audit_log", "audit_context", "access_log", "access_summary", "operation_log"}


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open(os.path.join(REPO, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    live.install(c)
    c.execute("INSERT INTO subjects (id, name, domain) VALUES (1, 'alpha', 'general')")
    c.commit()
    return c


def add_fact(con, statement="A fact.", visibility="private"):
    cur = con.execute("INSERT INTO facts (subject_id, statement, trust_level, freshness, visibility, source_key) "
                      "VALUES (1, ?, 'high', 'unreviewed', ?, ?)", (statement, visibility, "k-" + statement))
    con.commit()
    return cur.lastrowid


def add_revision(con, fact_id, revision=1, visibility="private"):
    con.execute("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, "
                "status, visibility) VALUES (?, 'k', ?, '2026-01-01T00:00:00+00:00', 'cli', 's', 'high', 'active', ?)",
                (fact_id, revision, visibility))
    con.commit()


def audit(con, table):
    return [dict(r) for r in con.execute("SELECT * FROM audit_log WHERE table_name = ? ORDER BY id", (table,))]


def test_install_is_idempotent_and_names_every_trigger(con):
    names = live.install(con)
    assert live.install(con) == names
    assert set(audit_rules.trigger_names()) <= set(names)
    assert {"fact_revisions_no_delete", "fact_revisions_update_guard"} <= set(names)
    in_db = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert set(names) <= in_db


def test_insert_update_delete_are_audited_with_the_whole_row(con):
    live.set_context(con, "cli", "s-1", "2026-11-04T10:00:00Z")
    fid = add_fact(con, "Original")
    con.execute("UPDATE facts SET statement = 'Changed' WHERE id = ?", (fid,))
    con.execute("DELETE FROM facts WHERE id = ?", (fid,))
    con.commit()
    rows = audit(con, "facts")
    assert [r["op"] for r in rows] == ["insert", "update", "delete"]
    assert all((r["via"], r["session_id"], r["changed_at"], r["row_key"]) == ("cli", "s-1", "2026-11-04T10:00:00Z", f"[{fid}]")
               for r in rows)
    assert rows[0]["old_json"] is None and '"statement":"Original"' in rows[0]["new_json"]
    assert '"statement":"Original"' in rows[1]["old_json"] and '"statement":"Changed"' in rows[1]["new_json"]
    assert rows[2]["new_json"] is None and '"statement":"Changed"' in rows[2]["old_json"]


def test_a_write_without_context_is_recorded_as_unknown_not_skipped(con):
    add_fact(con)
    (row,) = audit(con, "facts")
    assert row["via"] == "unknown" and row["session_id"] is None
    assert row["changed_at"].endswith("Z") and row["changed_at"].startswith("20")   # the SQL clock when no time is given


def test_clearing_the_context_stops_attributing_later_writes(con):
    live.set_context(con, "mcp")
    add_fact(con, "one")
    live.clear_context(con)
    add_fact(con, "two")
    assert [r["via"] for r in audit(con, "facts")] == ["mcp", "unknown"]


def test_a_composite_key_table_records_both_key_columns(con):
    fid = add_fact(con)
    con.execute("INSERT INTO sources (id, citekey, name, source_type) VALUES (1, 'c1', 'S', 'primary')")
    con.execute("INSERT INTO fact_sources (fact_id, source_id) VALUES (?, 1)", (fid,))
    con.commit()
    (row,) = audit(con, "fact_sources")
    assert row["row_key"] == f"[{fid},1]"


def test_the_audit_and_operation_logs_are_append_only(con):
    add_fact(con)
    con.execute("INSERT INTO operation_log (started_at, name, via, outcome) VALUES ('t', 'x', 'cli', 'ok')")
    con.commit()
    for sql in ("UPDATE audit_log SET via = 'x'", "DELETE FROM audit_log", "UPDATE operation_log SET name = 'y'",
                "DELETE FROM operation_log"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute(sql)


def test_the_access_log_can_be_pruned_but_never_edited(con):
    con.execute("INSERT INTO access_log (accessed_at, source_key, kind, via, mode) VALUES ('t', 'k', 'show', 'cli', 'private')")
    with pytest.raises(sqlite3.IntegrityError, match="never edited"):
        con.execute("UPDATE access_log SET kind = 'list'")
    con.execute("DELETE FROM access_log")
    with pytest.raises(sqlite3.IntegrityError):    # the vocabularies are enforced
        con.execute("INSERT INTO access_log (accessed_at, source_key, kind, via, mode) VALUES ('t', 'k', 'peek', 'cli', 'private')")


def test_revisions_cannot_be_edited_or_deleted(con):
    fid = add_fact(con)
    add_revision(con, fid)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE fact_revisions SET statement = 'rewritten'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM fact_revisions")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM facts WHERE id = ?", (fid,))      # the cascade would delete its history
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_a_revision_may_only_be_raised_to_private(con):
    fid = add_fact(con)
    add_revision(con, fid, 1, "normal")
    con.execute("UPDATE fact_revisions SET visibility = 'private'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE fact_revisions SET visibility = 'normal'")                # it cannot be lowered again
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE fact_revisions SET visibility = 'private', notes = 'x'")  # nor change anything with it


def test_the_privacy_floors_history_raise_still_works_on_a_live_database(con):
    fid = add_fact(con, "Quiet", "private")
    add_revision(con, fid, 1, "normal")
    privacy_store.SqlitePrivacyRepository(con).raise_history_of_private_facts()
    assert con.execute("SELECT visibility FROM fact_revisions").fetchone()[0] == "private"


def test_no_live_table_can_reach_the_normal_database():
    assert not LIVE_TABLES & set(normal_rules.TABLES)
    with open(os.path.join(REPO, "schema.sql"), encoding="utf-8") as f:
        text = f.read()
    assert not [t for t in LIVE_TABLES if t in text]       # the rebuild's schema does not know them


def test_a_database_without_some_curated_tables_installs_what_it_has():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, statement TEXT)")
    assert live.install(c) == ["audit_facts_insert", "audit_facts_update", "audit_facts_delete"]


def test_identifiers_and_keyless_tables_are_refused():
    with pytest.raises(ValueError):
        audit_rules.audit_trigger_sql("facts; DROP TABLE x", [("id", 1)])
    with pytest.raises(ValueError):
        audit_rules.audit_trigger_sql("facts", [("id; --", 1)])
    with pytest.raises(ValueError):
        audit_rules.audit_trigger_sql("facts", [("id", 0)])
