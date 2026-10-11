"""The SQLite adapter of the fact repository (fact_ingest_store), over the real schema.

One test per port method, plus a round trip of a whole FactRow and the check that the adapter has the shape of
the protocol it is wired to.
"""
import dataclasses
import os
import sqlite3

import pytest

import fact_ingest_decisions as decisions
import fact_ingest_store as store
import wiring
from test_add_fact import REPO


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    with open(os.path.join(REPO, "schema.sql")) as f:
        c.executescript(f.read())
    return c


def row(**kw):
    base = dict(statement="S.", is_original_claim=0, is_personal=1, trust_level="low", trust_rationale="why",
                date_added="2026-01-02", last_reviewed_at="2026-01-02", notes="n", recheck_by="2027-01-01",
                recheck_rationale=None, visibility="private", captured_via="cli", session_id="s", captured_at="t",
                source_quote="q", status="active", source_key="k-1", freshness="recheck",
                extracted_from_sha256="a" * 64, kind="decision", valid_from="2026", valid_to="2027-05",
                applies_to="adults", subject_id=None, origin_file_id=None)
    base.update(kw)
    return decisions.FactRow(**base)


def subject_id(con, name):
    """A subject row, as the subject repository would have made it (this adapter does not own subjects)."""
    return con.execute("INSERT INTO subjects (name) VALUES (?)", (name,)).lastrowid


def vault_file_id(con, path):
    return con.execute("INSERT INTO vault_files (path) VALUES (?)", (path,)).lastrowid


def test_the_adapter_has_the_shape_of_the_fact_repository_port():
    assert wiring.conformance_problems("facts", store) == []


def test_sources_are_listed_by_citekey_without_the_keyless_ones(con):
    con.execute("INSERT INTO sources (citekey, name, source_type) VALUES ('a', 'A', 'primary'), (NULL, 'B', 'primary')")
    ids = store.source_ids_by_citekey(con)
    assert list(ids) == ["a"] and ids["a"] == con.execute("SELECT id FROM sources WHERE citekey = 'a'").fetchone()[0]


def test_the_insert_names_exactly_the_fields_of_a_fact_row():
    import re
    columns = re.search(r"INSERT INTO facts \(([^)]*)\)", store._INSERT_FACT).group(1)
    params = re.findall(r":(\w+)", store._INSERT_FACT)
    fields = [f.name for f in dataclasses.fields(decisions.FactRow)]
    assert sorted(c.strip() for c in columns.split(",")) == sorted(fields) == sorted(params)


def test_a_fact_row_round_trips_every_column(con):
    subject = subject_id(con, "coffee")
    note = vault_file_id(con, "/n.md")
    fact_id = store.insert_fact(con, row(subject_id=subject, origin_file_id=note))
    got = dict(con.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone())
    expected = dataclasses.asdict(row(subject_id=subject, origin_file_id=note))
    assert {k: got[k] for k in expected} == expected
    assert got["provided_by"] == "user"
    assert store.fact_exists(con, "k-1") and not store.fact_exists(con, "other")


def test_a_revision_row_is_written_for_the_fact(con):
    import revisions
    subject = subject_id(con, "coffee")
    fact_id = store.insert_fact(con, row(subject_id=subject))
    entry = {"statement": "S.", "trust_level": "low", "freshness": "recheck", "recheck_by": "2027-01-01", "date_added": "2026-01-02"}
    store.insert_revision(con, fact_id, revisions.implicit_revision("k-1", entry, "2026-01-02", "general_facts.json"))
    got = con.execute("SELECT fact_id, source_key, revision, changed_via FROM fact_revisions").fetchone()
    assert tuple(got) == (fact_id, "k-1", 1, "original")


def test_a_source_link_is_written_once(con):
    subject = subject_id(con, "coffee")
    fact_id = store.insert_fact(con, row(subject_id=subject))
    con.execute("INSERT INTO sources (citekey, name, source_type) VALUES ('a', 'A', 'primary')")
    source_id = store.source_ids_by_citekey(con)["a"]
    store.link_source(con, fact_id, source_id, "p. 1", "q")
    store.link_source(con, fact_id, source_id, "p. 2", "q2")
    assert [tuple(r) for r in con.execute("SELECT locator, quote FROM fact_sources")] == [("p. 1", "q")]


def test_facts_are_counted_by_the_allowed_columns_only(con):
    subject = subject_id(con, "coffee")
    store.insert_fact(con, row(subject_id=subject, source_key="a"))
    store.insert_fact(con, row(subject_id=subject, source_key="b", trust_level="high"))
    assert store.facts_by(con, "trust_level") == {"high": 1, "low": 1}
    with pytest.raises(ValueError):
        store.facts_by(con, "statement; DROP TABLE facts")


def test_commit_makes_the_rows_durable(tmp_path):
    path = str(tmp_path / "k.db")
    c = sqlite3.connect(path)
    with open(os.path.join(REPO, "schema.sql")) as f:
        c.executescript(f.read())
    subject_id(c, "coffee")
    store.commit(c)
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 1
