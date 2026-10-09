"""facts.py and seed_general_facts.py: what they write into `facts`.

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
    for m in ("paths", "local_paths", "shared", "add_fact"):
        sys.modules.pop(m, None)
    monkeypatch.syspath_prepend(REPO)

    def load(name):
        spec = importlib.util.spec_from_file_location("ing_" + name, os.path.join(REPO, "ingest", name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    class Ctx:
        env = e
        mod04 = load("facts.py")
        mod11 = load("seed_general_facts.py")

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

        @staticmethod
        def stamped(items, date):
            """Entries as the data holds them after #35: an entry without `date_added` is a build
            error, so fixtures get one unless a test passes stamp=False to prove that error."""
            return [{"date_added": date, **e} for e in items]

        @classmethod
        def run04(cls, items, con=None, stamp=True):
            con = con or cls.db()
            cls.write("pilot_facts.json", cls.stamped(items, LEGACY_04) if stamp else items)
            for i in range(1, 5):
                cls.write(f"facts_batch{i}.json", [])
            cls.mod04.DATA_DIR = e.data_dir
            cls.mod04.run(con)
            return con

        @classmethod
        def run11(cls, items, con=None, stamp=True):
            con = con or cls.db()
            # #7: general_facts.json is never legacy, so an entry needs a freshness; fixtures that
            # are about something else get no-decay (a test passes its own to override).
            items = [{"freshness": "no-decay", "recheck_rationale": "no decay", **e} for e in items]
            cls.write("general_facts.json", cls.stamped(items, LEGACY_11) if stamp else items)
            cls.mod11.DATA_DIR = e.data_dir
            cls.mod11.run(con)
            return con

    return Ctx


def F(**kw):
    base = dict(subject="s", statement="A statement.", trust_level="low")
    base.update(kw)
    return base


# ------------------------------------------------ characterization: dates

def test_04_and_11_store_the_entrys_own_date_for_date_added_and_last_reviewed(ingest):
    con = ingest.run04([F(date_added=LEGACY_04)])
    r = con.execute("SELECT date_added, last_reviewed_at FROM facts").fetchone()
    assert (r["date_added"], r["last_reviewed_at"]) == (LEGACY_04, LEGACY_04)
    con = ingest.run11([F(date_added=LEGACY_11)])
    r = con.execute("SELECT date_added, last_reviewed_at FROM facts").fetchone()
    assert (r["date_added"], r["last_reviewed_at"]) == (LEGACY_11, LEGACY_11)
    assert con.execute("SELECT changed_at FROM fact_revisions").fetchone()[0] == LEGACY_11


# ------------------------------------------------ #35: no date in code, an undated entry is a loud build error

@pytest.mark.parametrize("run, fname", [("run04", "pilot_facts.json"), ("run11", "general_facts.json")])
def test_an_entry_without_date_added_fails_the_build_naming_file_and_entry(ingest, run, fname):
    items = [F(date_added=LEGACY_04), F(statement="The undated one.")]
    with pytest.raises(ValueError) as e:
        getattr(ingest, run)(items, stamp=False)
    msg = str(e.value)
    assert f"{fname}[1]" in msg and "The undated one." in msg and "no `date_added`" in msg
    assert "ingest.backfill_dates" in msg


@pytest.mark.parametrize("run", ["run04", "run11"])
@pytest.mark.parametrize("bad", [None, "", "  ", "11/09/2026", "yesterday", 20260911])
def test_a_null_blank_or_non_iso_date_added_is_an_error_not_a_default(ingest, run, bad):
    with pytest.raises(ValueError, match="invalid `date_added`"):
        getattr(ingest, run)([F(date_added=bad)], stamp=False)


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_an_undated_entry_that_would_be_skipped_anyway_does_not_trip_the_date_check(ingest, run):
    con = getattr(ingest, run)([F(statement="  "), F(date_added=LEGACY_04)], stamp=False)
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_no_row_is_written_for_the_run_that_failed_on_a_missing_date_nothing_invented(ingest, run):
    con = ingest.db()
    with pytest.raises(ValueError):
        getattr(ingest, run)([F()], con=con, stamp=False)
    assert con.execute("SELECT COUNT(*) FROM facts WHERE date_added IS NULL OR date_added = ''").fetchone()[0] == 0


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


def test_04_honors_each_entrys_stored_date_added(ingest):
    con = ingest.run04([F(date_added="2026-10-03T12:00:00+00:00"), F(statement="Other.", date_added=LEGACY_04)])
    got = [r[0] for r in con.execute("SELECT date_added FROM facts ORDER BY id")]
    assert got == ["2026-10-03T12:00:00+00:00", LEGACY_04]


def test_no_hardcoded_date_constants_left_in_the_ingest_scripts():
    import re
    for name in ("measurements.py", "facts.py", "seed_general_facts.py"):
        text = open(os.path.join(REPO, "ingest" if os.path.exists(os.path.join(REPO, "ingest", name)) else "", name)).read()
        assert "TODAY =" not in text and "TODAY=" not in text, name
        assert "LEGACY_DATE_ADDED" not in text, name
        # nor any literal ISO date assigned or used as a default
        assert not re.search(r"""=\s*["']\d{4}-\d\d-\d\d""", text), name
        assert not re.search(r"""or\s+["']\d{4}-\d\d-\d\d""", text), name


def test_a_fact_added_today_keeps_its_date_across_a_rebuild_on_a_later_day(ingest):
    import clock
    from add_fact import NewFact, append_fact
    path = os.path.join(ingest.env.data_dir, "general_facts.json")
    with clock.frozen("2026-10-03T08:00:00+00:00"):
        res = append_fact(NewFact(statement="Kept.", subject="s", trust_level="low", no_decay=True, recheck_rationale="no decay"), data_path=path,
                          db_path=os.path.join(ingest.env.root, "none.db"))
    assert res.ok
    with clock.frozen("2027-03-01T00:00:00+00:00"):
        ingest.mod11.DATA_DIR = ingest.env.data_dir
        con = ingest.db()
        ingest.mod11.run(con)
    assert con.execute("SELECT date_added FROM facts").fetchone()[0] == "2026-10-03T08:00:00+00:00"
