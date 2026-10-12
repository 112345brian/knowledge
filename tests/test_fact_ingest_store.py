"""The SQLite adapters of the fact ingest (fact_ingest_store), over the real schema.

`SqliteFactRepository` is `build_repositories_port.FactRepository` less `link_measurements` (client logic): one test per method, plus a round trip of a whole
`NewFact` and the check that each adapter has the shape of the protocol it is wired to.
"""
import dataclasses
import os
import re
import sqlite3

import pytest

import build_records as r
import fact_ingest_store as store
import wiring
from test_add_fact import REPO


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON;")
    with open(os.path.join(REPO, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    return c


def fact(subject, **kw):
    base = dict(subject_id=subject, statement="S.", is_original_claim=0, is_personal=1, trust_level="low",
                trust_rationale="why", date_added="2026-01-02", visibility="private", status="active",
                source_key="k-1", freshness="recheck", kind="decision", notes="n", recheck_by="2027-01-01",
                recheck_rationale=None, origin_file_id=None, captured_via="cli", session_id="s", captured_at="t",
                source_quote="q", extracted_from_sha256="a" * 64, valid_from="2026", valid_to="2027-05",
                applies_to="adults")
    base.update(kw)
    return r.NewFact(**base)


def subject_id(con, name):
    """A subject row, as the subject repository would have made it (this adapter does not own subjects)."""
    return r.SubjectId(con.execute("INSERT INTO subjects (name) VALUES (?)", (name,)).lastrowid)


def vault_file_id(con, path):
    return r.VaultFileId(con.execute("INSERT INTO vault_files (path) VALUES (?)", (path,)).lastrowid)


@pytest.mark.parametrize("name,adapter", [("facts", store.SqliteFactRepository), ("revision_writer", store.SqliteFactRevisions),
                                          ("unit_of_work", store.SqliteUnitOfWork)])
def test_each_adapter_has_the_shape_of_its_port(name, adapter):
    assert wiring.conformance_problems(name, adapter(sqlite3.connect(":memory:"))) == []


def test_the_insert_names_every_new_fact_field_and_the_one_column_the_foundation_derives():
    columns = re.search(r"INSERT INTO facts \(([^)]*)\)", store._INSERT_FACT).group(1)
    params = re.findall(r":(\w+)", store._INSERT_FACT)
    fields = [f.name for f in dataclasses.fields(r.NewFact)]
    assert sorted(c.strip() for c in columns.split(",")) == sorted(params) == sorted(fields + ["last_reviewed_at"])


def test_a_fact_round_trips_every_column_and_is_last_reviewed_the_day_it_was_added(con):
    repo = store.SqliteFactRepository(con)
    subject = subject_id(con, "coffee")
    note = vault_file_id(con, "/n.md")
    new = fact(subject, origin_file_id=note)
    fact_id = repo.add(new)
    got = dict(con.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone())
    expected = dataclasses.asdict(new)
    assert {k: got[k] for k in expected} == expected
    assert got["last_reviewed_at"] == "2026-01-02" and got["provided_by"] == "user"
    assert repo.source_key_exists("k-1") and not repo.source_key_exists("other")
    assert repo.find_by_source_key("k-1") == fact_id and repo.find_by_source_key("other") is None


def test_ids_are_allocated_in_call_order_and_a_repeated_key_raises(con):
    repo = store.SqliteFactRepository(con)
    subject = subject_id(con, "coffee")
    ids = [repo.add(fact(subject, source_key=k)) for k in ("c", "a", "b")]
    assert ids == [1, 2, 3]
    with pytest.raises(sqlite3.IntegrityError):
        repo.add(fact(subject, source_key="a"))


def test_a_revision_row_is_written_for_the_fact(con):
    import revisions
    subject = subject_id(con, "coffee")
    fact_id = store.SqliteFactRepository(con).add(fact(subject))
    entry = {"statement": "S.", "trust_level": "low", "freshness": "recheck", "recheck_by": "2027-01-01", "date_added": "2026-01-02"}
    store.SqliteFactRevisions(con).insert(fact_id, revisions.implicit_revision("k-1", entry, "2026-01-02", "general_facts.json"))
    got = con.execute("SELECT fact_id, source_key, revision, changed_via FROM fact_revisions").fetchone()
    assert tuple(got) == (fact_id, "k-1", 1, "original")


def test_a_source_link_is_written_once(con):
    repo = store.SqliteFactRepository(con)
    fact_id = repo.add(fact(subject_id(con, "coffee")))
    source_id = r.SourceId(con.execute("INSERT INTO sources (citekey, name, source_type) VALUES ('a', 'A', 'primary')").lastrowid)
    repo.link_source(r.FactSourceLink(fact_id, source_id, "p. 1", "q"))
    repo.link_source(r.FactSourceLink(fact_id, source_id, "p. 2", "q2"))
    assert [tuple(row) for row in con.execute("SELECT locator, quote FROM fact_sources")] == [("p. 1", "q")]


def test_facts_are_tallied_by_the_allowed_columns_only_in_value_order(con):
    repo = store.SqliteFactRepository(con)
    subject = subject_id(con, "coffee")
    repo.add(fact(subject, source_key="a"))
    repo.add(fact(subject, source_key="b", trust_level="high"))
    repo.add(fact(subject, source_key="c", trust_level="high", is_personal=0))
    assert list(repo.tally("trust_level").items()) == [("high", 2), ("low", 1)]
    assert list(repo.tally("is_personal").items()) == [(0, 1), (1, 2)]
    with pytest.raises(ValueError):
        repo.tally("statement; DROP TABLE facts")  # type: ignore[arg-type]


def test_commit_makes_the_rows_durable(tmp_path):
    path = str(tmp_path / "k.db")
    c = sqlite3.connect(path)
    with open(os.path.join(REPO, "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    subject_id(c, "coffee")
    store.SqliteUnitOfWork(c).commit()
    other = sqlite3.connect(path)
    assert other.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 1
    other.close()
    c.close()
