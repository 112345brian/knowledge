"""04_ingest_facts.py and 11_seed_general_facts.py: what they write into `facts`.

Characterization first (issues #19/#20/#24 change these rows), then the new guarantees.
Fixture JSON only; nothing here reads the real knowledge-private data.
"""
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

from test_add_fact import Env, REPO

LEGACY_04 = "2026-09-11"
LEGACY_11 = "2026-09-26"


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", e.private)
    for m in ("paths", "local_paths", "_shared", "add_fact"):
        sys.modules.pop(m, None)
    monkeypatch.syspath_prepend(REPO)

    def load(name):
        spec = importlib.util.spec_from_file_location("ing_" + name, os.path.join(REPO, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    class Ctx:
        env = e
        mod04 = load("04_ingest_facts.py")
        mod11 = load("11_seed_general_facts.py")

        @staticmethod
        def write(name, items):
            with open(os.path.join(e.data_dir, name), "w") as f:
                json.dump(items, f)

        @staticmethod
        def db():
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON;")
            con.executescript(open(os.path.join(REPO, "schema.sql")).read())
            return con

        @classmethod
        def run04(cls, items, con=None):
            con = con or cls.db()
            cls.write("pilot_facts.json", items)
            for i in range(1, 5):
                cls.write(f"facts_batch{i}.json", [])
            cls.mod04.DATA_DIR = e.data_dir
            cls.mod04.run(con)
            return con

        @classmethod
        def run11(cls, items, con=None):
            con = con or cls.db()
            cls.write("general_facts.json", items)
            cls.mod11.DATA_DIR = e.data_dir
            cls.mod11.run(con)
            return con

    return Ctx


def F(**kw):
    base = dict(subject="s", statement="A statement.", trust_level="low")
    base.update(kw)
    return base


# ------------------------------------------------ characterization: dates

def test_04_without_a_date_uses_the_legacy_default_for_date_added_and_last_reviewed(ingest):
    con = ingest.run04([F()])
    r = con.execute("SELECT date_added, last_reviewed_at FROM facts").fetchone()
    assert (r["date_added"], r["last_reviewed_at"]) == (LEGACY_04, LEGACY_04)


def test_11_without_a_date_uses_the_legacy_default_for_date_added_and_last_reviewed(ingest):
    con = ingest.run11([F()])
    r = con.execute("SELECT date_added, last_reviewed_at FROM facts").fetchone()
    assert (r["date_added"], r["last_reviewed_at"]) == (LEGACY_11, LEGACY_11)


def test_the_date_columns_reach_the_views(ingest):
    con = ingest.run04([F()])
    assert con.execute("SELECT date_added FROM v_facts").fetchone()[0] == LEGACY_04
    assert con.execute("SELECT date_added FROM fact_with_sources").fetchone()[0] == LEGACY_04


def test_ingest_still_skips_bad_shapes(ingest):
    con = ingest.run11([F(statement="  "), F(subject=""), F(trust_level="bogus"), F()])
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_is_personal_default_and_override_in_11(ingest):
    con = ingest.run11([F(), F(statement="Other.", is_personal=False)])
    got = [r[0] for r in con.execute("SELECT is_personal FROM facts ORDER BY id")]
    assert got == [1, 0]


def test_fts_triggers_index_new_rows(ingest):
    con = ingest.run11([F(statement="Zebra crossing facts.")])
    assert con.execute("SELECT COUNT(*) FROM facts_fts WHERE facts_fts MATCH 'zebra'").fetchone()[0] == 1


# ------------------------------------------------ #19 new guarantees

def test_11_honors_a_stored_date_added_and_uses_it_for_last_reviewed(ingest):
    con = ingest.run11([F(date_added="2026-10-03T12:00:00+00:00")])
    r = con.execute("SELECT date_added, last_reviewed_at FROM facts").fetchone()
    assert r["date_added"] == "2026-10-03T12:00:00+00:00"
    assert r["last_reviewed_at"] == "2026-10-03T12:00:00+00:00"


def test_04_honors_a_stored_date_added_where_one_exists(ingest):
    con = ingest.run04([F(date_added="2026-10-03T12:00:00+00:00"), F(statement="Undated.")])
    got = [r[0] for r in con.execute("SELECT date_added FROM facts ORDER BY id")]
    assert got == ["2026-10-03T12:00:00+00:00", LEGACY_04]


def test_no_hardcoded_TODAY_left_in_the_fact_ingest_scripts():
    for name in ("04_ingest_facts.py", "11_seed_general_facts.py"):
        assert "TODAY" not in open(os.path.join(REPO, name)).read(), name


def test_a_fact_added_today_keeps_its_date_across_a_rebuild_on_a_later_day(ingest):
    import clock
    from add_fact import NewFact, append_fact
    path = os.path.join(ingest.env.data_dir, "general_facts.json")
    with clock.frozen("2026-10-03T08:00:00+00:00"):
        res = append_fact(NewFact(statement="Kept.", subject="s", trust_level="low"), data_path=path,
                          db_path=os.path.join(ingest.env.root, "none.db"))
    assert res.ok
    with clock.frozen("2027-03-01T00:00:00+00:00"):
        ingest.mod11.DATA_DIR = ingest.env.data_dir
        con = ingest.db()
        ingest.mod11.run(con)
    assert con.execute("SELECT date_added FROM facts").fetchone()[0] == "2026-10-03T08:00:00+00:00"
