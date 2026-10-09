"""#40: valid time on facts (valid_from / valid_to).

The library (validtime), the schema CHECKs, the writers (add_fact, add_facts), ingest, revisions
(correcting validity appends a revision; show --as-of), `--valid-at` on search/facts/show in both
knowledge.py and modes.py, the normal-only DB, and the rule that a past valid_to is not retraction.
"""
import json
import os
import sqlite3
import sys

import pytest

import validtime
from test_add_fact import REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _restore_modules():
    before = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


# ------------------------------------------------------------------ the library

@pytest.mark.parametrize("v", ["2024", "2024-03", "2024-03-15", "2024-02-29", "0001", "9999-12-31"])
def test_real_boundaries_are_valid(v):
    assert validtime.is_valid_boundary(v)


@pytest.mark.parametrize("v", ["", "24", "2024-3", "2024-13", "2024-00", "2023-02-29", "2024-04-31", "2024-03-15T10:00",
                               "2024/03/15", " 2024", "0000", None, 2024, "２０２４"])
def test_bad_boundaries_are_rejected(v):
    assert not validtime.is_valid_boundary(v)


def test_problems_ranges():
    assert validtime.problems(None, None) == []
    assert validtime.problems("2024", None) == [] and validtime.problems(None, "2024") == []
    assert validtime.problems("2024-03-15", "2024-03-15") == []      # a point in time
    assert validtime.problems("2024", "2024-03") == []               # partial: end of March is inside 2024
    assert validtime.problems("2024-03-15", "2024-03") == []         # to ends 03-31, after the 15th
    assert validtime.problems("2024-03", "2024-03-01") == []
    for a, b in (("2024-05", "2024-03"), ("2024-03-15", "2024-03-14"), ("2025", "2024-12-31"), ("2024-04", "2024-03-31")):
        assert any("before" in m for m in validtime.problems(a, b)), (a, b)
    assert validtime.problems("2024-13", None) and validtime.problems(None, "x")
    assert len(validtime.problems("bad", "worse")) == 2


def test_contains_closed_open_and_partial_boundaries():
    assert validtime.contains("2024-03-01", "2024-03-31", "2024-03-01")
    assert validtime.contains("2024-03-01", "2024-03-31", "2024-03-31")
    assert not validtime.contains("2024-03-01", "2024-03-31", "2024-02-29")
    assert not validtime.contains("2024-03-01", "2024-03-31", "2024-04-01")
    assert validtime.contains(None, None, "1999-01-01")                       # no interval: always
    assert validtime.contains("2024-03-01", None, "2090-01-01")               # open-ended future
    assert not validtime.contains("2024-03-01", None, "2024-02-29")
    assert validtime.contains(None, "2024-03-01", "1900-01-01") and not validtime.contains(None, "2024-03-01", "2024-03-02")
    # partial dates name the whole span
    assert validtime.contains("2024", "2024", "2024-01-01") and validtime.contains("2024", "2024", "2024-12-31")
    assert not validtime.contains("2024", "2024", "2023-12-31") and not validtime.contains("2024", "2024", "2025-01-01")
    assert validtime.contains("2024-03", "2024-03", "2024-03-31") and not validtime.contains("2024-03", "2024-03", "2024-04-01")
    assert validtime.contains("2023-06", "2024", "2024-12-31")
    # a point in time
    assert validtime.contains("2024-03-15", "2024-03-15", "2024-03-15") and not validtime.contains("2024-03-15", "2024-03-15", "2024-03-16")


@pytest.mark.parametrize("at", ["2024", "2024-03", "2024-02-30", "", None, "2024-03-15T00:00"])
def test_contains_rejects_a_bad_at(at):
    with pytest.raises(ValueError):
        validtime.contains(None, None, at)


def test_contains_rejects_bad_bounds():
    with pytest.raises(ValueError):
        validtime.contains("2024-05", "2024-03", "2024-04-01")


def test_sql_and_python_agree_on_every_combination():
    con = sqlite3.connect(":memory:")
    bounds = [None, "2023", "2023-12", "2024", "2024-03", "2024-03-15", "2024-12-31", "2025"]
    ats = ["2022-06-01", "2023-01-01", "2023-12-31", "2024-01-01", "2024-03-14", "2024-03-15", "2024-03-31", "2024-04-01",
           "2024-12-31", "2025-01-01", "2025-12-31", "2026-01-01"]
    con.execute("CREATE TABLE t (valid_from TEXT, valid_to TEXT)")
    for a in bounds:
        for b in bounds:
            if validtime.problems(a, b):
                continue
            con.execute("DELETE FROM t")
            con.execute("INSERT INTO t VALUES (?, ?)", (a, b))
            for at in ats:
                got = con.execute(f"SELECT {validtime.sql_valid_at('t.valid_from', 't.valid_to')} FROM t", (at, at)).fetchone()[0]
                assert bool(got) == validtime.contains(a, b, at), (a, b, at)


def test_describe():
    assert validtime.describe(None, None) == ""
    assert validtime.describe("2024-03", None) == "2024-03 .. (open)"
    assert validtime.describe(None, "2024") == "(unknown) .. 2024"
    assert validtime.describe("2024-03-15", "2024-03-15") == "2024-03-15"


# ------------------------------------------------------------------ schema

def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (id, name, domain) VALUES (1, 's', 'g')")
    return con


def _ins(con, vf, vt, n=[0]):
    n[0] += 1
    con.execute("INSERT INTO facts (subject_id, statement, trust_level, freshness, valid_from, valid_to, source_key) "
                "VALUES (1, 'x', 'low', 'unreviewed', ?, ?, ?)", (vf, vt, f"k{n[0]}"))


def test_schema_accepts_the_shapes_and_rejects_bad_shapes_and_ranges():
    con = _con()
    for vf, vt in ((None, None), ("2024", None), (None, "2024-03"), ("2024-03", "2024-03-15"), ("2024-03-15", "2024-03-15")):
        _ins(con, vf, vt)
    for vf, vt in (("24", None), ("2024-3", None), (None, "2024-03-15T10:00"), ("x", "y"), ("2024-05", "2024-03"),
                   ("2024-03-15", "2024-03-14"), ("", None)):
        with pytest.raises(sqlite3.IntegrityError):
            _ins(con, vf, vt)


def test_schema_rejects_a_bad_range_on_revisions_too():
    con = _con()
    _ins(con, None, None)
    base = ("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, "
            "status, visibility, valid_from, valid_to) VALUES (1, 'k1', 1, 't', 'v', 's', 'low', 'active', 'private', ?, ?)")
    con.execute(base, ("2024", "2024-03"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(base.replace("'k1', 1", "'k1', 2"), ("2024-05", "2024-03"))


# ------------------------------------------------------------------ writers

def nf(**kw):
    import add_fact
    base = dict(statement="S.", subject="x", trust_level="low", no_decay=True, recheck_rationale="r")
    base.update(kw)
    return add_fact.NewFact(**base)


def test_add_fact_validates_and_writes_the_interval():
    import add_fact
    assert add_fact.validate_fact(nf(valid_from="2024-03", valid_to="2024-05-31"), db_path="/nonexistent")[0] == []
    e = add_fact.build_entry(nf(valid_from="2024-03", valid_to="2024-05-31"))
    assert (e["valid_from"], e["valid_to"]) == ("2024-03", "2024-05-31")
    e = add_fact.build_entry(nf())
    assert "valid_from" not in e and "valid_to" not in e
    errs = add_fact.validate_fact(nf(valid_from="2024-05", valid_to="2024-03"), db_path="/nonexistent")[0]
    assert any("before" in m for m in errs)
    assert add_fact.validate_fact(nf(valid_from="2023-02-29"), db_path="/nonexistent")[0]
    assert add_fact.validate_fact(nf(valid_to=""), db_path="/nonexistent")[0]


def test_cli_flags_round_trip_and_bad_range_is_refused(tmp_path):
    from test_add_fact import Env
    env = Env(tmp_path)
    r = env.cli("Lived at X.", "--subject", "x", "--trust", "low", "--valid-from", "2019", "--valid-to", "2024-06")
    assert r.returncode == 0, r.stderr
    assert (env.entries()[0]["valid_from"], env.entries()[0]["valid_to"]) == ("2019", "2024-06")
    r = env.cli("Bad.", "--subject", "x", "--trust", "low", "--valid-from", "2024-06", "--valid-to", "2019")
    assert r.returncode == 1 and "before" in r.stderr and len(env.entries()) == 1


def test_batch_items_take_validity():
    import facts_batch
    assert {"valid_from", "valid_to"} <= facts_batch.ITEM_KEYS


# ------------------------------------------------------------------ ingest + revisions

def test_ingest_stores_validity_and_defaults_to_null(ingest):
    con = ingest.run04([F(valid_from="2024-03"), F(statement="No validity.")])
    assert [tuple(r) for r in con.execute("SELECT valid_from, valid_to FROM facts ORDER BY id")] == [("2024-03", None), (None, None)]
    assert [tuple(r) for r in con.execute("SELECT valid_from, valid_to FROM fact_revisions ORDER BY id")] == [("2024-03", None), (None, None)]
    con = ingest.run11([F(valid_from="2020", valid_to="2020")])
    assert tuple(con.execute("SELECT valid_from, valid_to FROM facts").fetchone()) == ("2020", "2020")


@pytest.mark.parametrize("kw", [dict(valid_from="2023-02-29"), dict(valid_to="2024-13"), dict(valid_from="2024-05", valid_to="2024-03"),
                                dict(valid_from=""), dict(valid_to=5)])
def test_ingest_fails_loudly_on_bad_validity(ingest, kw):
    for run in (ingest.run04, ingest.run11):
        with pytest.raises(Exception, match="valid_") as e:  # by name: the fixture reloads modules
            run([F(**kw)])
        assert type(e.value).__name__ in ("RevisionError", "IntegrityError")


def test_revision_records_carry_and_check_validity():
    import revisions
    snap = revisions.entry_snapshot({"statement": "x", "trust_level": "low", "freshness": "no-decay", "recheck_rationale": "r", "valid_from": "2024"})
    assert (snap["valid_from"], snap["valid_to"]) == ("2024", None)
    rec = {"source_key": "k-one", "revision": 2, "changed_at": "2026-02-01T00:00:00+00:00", "changed_via": "cli",
           "session_id": None, "change_reason": "corrected", **snap, "valid_to": "2025"}
    rec = {k: rec[k] for k in revisions.REVISION_KEYS}
    assert revisions.validate_record_shape(rec) == []
    assert revisions.validate_record_shape(dict(rec, valid_to="2023"))
    assert revisions.validate_record_shape(dict(rec, valid_from="2024-02-30"))
    assert any("missing key" in e for e in revisions.validate_record_shape({k: v for k, v in rec.items() if k != "valid_to"}))


# ------------------------------------------------------------------ read side

def _facts_db():
    con = _con()
    rows = [("2024-01-01", "2024-03-31", "normal"),   # 1 past interval
            (None, None, "normal"),                    # 2 no interval
            ("2024-06", None, "normal"),               # 3 open-ended
            ("2023", "2023", "private"),               # 4 a whole past year, private
            ("2024-03-15", "2024-03-15", "normal")]    # 5 a point in time
    for i, (a, b, vis) in enumerate(rows, start=1):
        con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, valid_from, valid_to, visibility, source_key) "
                    "VALUES (?, 1, ?, 'low', 'unreviewed', ?, ?, ?, ?)", (i, f"banana fact {i}", a, b, vis, f"k{i}"))
    return con


def test_knowledge_valid_at_inside_outside_open_ended_and_point():
    import knowledge
    con = _facts_db()
    ids = lambda at: [r["id"] for r in knowledge.list_facts(con, valid_at=at)]  # noqa: E731
    assert ids("2024-02-01") == [1, 2]
    assert ids("2024-04-01") == [2]            # outside fact 1's interval
    assert ids("2024-03-15") == [1, 2, 5]
    assert ids("2024-03-16") == [1, 2]
    assert ids("2030-01-01") == [2, 3]         # open-ended is still true
    assert ids("2023-07-01") == [2, 4]
    assert [r["id"] for r in knowledge.search_facts(con, "banana", valid_at="2024-06-01")] == [2, 3] or \
        {r["id"] for r in knowledge.search_facts(con, "banana", valid_at="2024-06-01")} == {2, 3}
    assert [r["id"] for r in knowledge.list_facts(con)] == [1, 2, 3, 4, 5]    # no --valid-at: no filter
    for bad in ("2024", "2024-02-30", "", "x' OR '1'='1"):
        with pytest.raises(ValueError):
            knowledge.list_facts(con, valid_at=bad)
    row = knowledge.list_facts(con)[0]
    assert (row["valid_from"], row["valid_to"]) == ("2024-01-01", "2024-03-31")


def test_modes_valid_at_keeps_the_gate():
    import modes
    con = _facts_db()
    normal, private = modes.Session(mode=modes.Mode.normal), modes.Session(mode=modes.Mode.private)
    assert [r["id"] for r in modes.list_facts(normal, con, valid_at="2023-07-01")] == [2]      # fact 4 is private
    assert [r["id"] for r in modes.list_facts(private, con, valid_at="2023-07-01")] == [2, 4]
    assert [r["id"] for r in modes.list_facts(normal, con, valid_at="2024-03-15")] == [1, 2, 5]
    assert {r["id"] for r in modes.search_facts(normal, con, "banana", valid_at="2024-07-01")} == {2, 3}
    with pytest.raises(ValueError):
        modes.list_facts(normal, con, valid_at="2024")
    with pytest.raises(ValueError):
        modes.search_facts(normal, con, "banana", valid_at="nope")


def test_cli_valid_at_on_facts_search_and_show(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    env.sql("UPDATE facts SET valid_from = '2024-01', valid_to = '2024-03' WHERE id = 1")
    ids = lambda *a: [r["id"] for r in json.loads(env.cli(*a, "--json").stdout)]  # noqa: E731
    assert 1 in ids("facts", "--valid-at", "2024-02-10")
    assert 1 not in ids("facts", "--valid-at", "2024-04-01")
    assert 1 in ids("facts", "--valid-at", "2024-03-31") and 1 not in ids("facts", "--valid-at", "2023-12-31")
    assert 1 in ids("search", "protein", "--valid-at", "2024-02-10")
    assert 1 not in ids("search", "protein", "--valid-at", "2025-01-01")
    r = env.cli("facts", "--valid-at", "2024-02")
    assert r.returncode == 1 and "valid_at" in r.stderr
    r = env.cli("show", "1", "--valid-at", "2024-02-10")
    assert r.returncode == 0 and "Valid: 2024-01 .. 2024-03" in r.stdout and "Valid on 2024-02-10: yes" in r.stdout
    r = env.cli("show", "1", "--valid-at", "2025-01-01")
    assert r.returncode == 1 and "Valid on 2025-01-01: NO" in r.stdout
    r = env.cli("show", "1", "--valid-at", "2025-01-01", "--json")
    assert r.returncode == 1 and json.loads(r.stdout)["valid_at"] == {"date": "2025-01-01", "valid": False}
    assert env.cli("show", "1", "--valid-at", "2025").returncode == 1
    assert "Valid" not in env.cli("show", "2").stdout           # no interval: no line
    assert env.cli("show", "1").returncode == 0                 # without --valid-at, exit 0 even if long past


def test_show_as_of_is_transaction_time_valid_at_is_valid_time(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    env.sql("UPDATE fact_revisions SET valid_from = '2020' WHERE source_key = 'k-one' AND revision = 1")
    env.sql("UPDATE fact_revisions SET valid_from = '2020', valid_to = '2022' WHERE source_key = 'k-one' AND revision = 2")
    old = env.cli("show", "1", "--as-of", "2026-03-01")           # believed in March 2026: open-ended
    new = env.cli("show", "1", "--as-of", "2026-07-01")           # corrected in June 2026: ended 2022
    assert "Valid: 2020 .. (open)" in old.stdout and "Valid: 2020 .. 2022" in new.stdout
    assert env.cli("show", "1", "--as-of", "2026-03-01", "--valid-at", "2024-01-01").returncode == 0
    r = env.cli("show", "1", "--as-of", "2026-07-01", "--valid-at", "2024-01-01")
    assert r.returncode == 1 and "Valid on 2024-01-01: NO" in r.stdout


# ------------------------------------------------------------------ validity is not retraction

def test_a_past_valid_to_does_not_change_status_or_trip_the_premise_audit():
    import claims_audit
    con = _facts_db()
    con.execute("INSERT INTO claims (id, statement) VALUES (1, 'c')")
    con.execute("INSERT INTO claim_facts (claim_id, fact_id) VALUES (1, 1)")
    assert con.execute("SELECT status FROM facts WHERE id = 1").fetchone()[0] == "active"   # valid_to long past
    assert claims_audit.audit_claims(con, today="2030-01-01") == []


# ------------------------------------------------------------------ normal-only DB

def test_normal_db_includes_validity_with_the_same_checks():
    import leak_test
    import normal_db
    import privacy
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("UPDATE facts SET valid_from = '2024-03', valid_to = '2024-05-31' WHERE id = 1")
        c.execute("UPDATE fact_revisions SET valid_from = '2024-03' WHERE fact_id = 1 AND revision = 1")
        c.commit()
        c.close()
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        con = sqlite3.connect(path)
        assert tuple(con.execute("SELECT valid_from, valid_to FROM facts WHERE id = 1").fetchone()) == ("2024-03", "2024-05-31")
        assert tuple(con.execute("SELECT valid_from, valid_to FROM facts WHERE id = 2").fetchone()) == (None, None)
        assert con.execute("SELECT valid_from FROM fact_revisions WHERE fact_id = 1 AND revision = 1").fetchone()[0] == "2024-03"
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE facts SET valid_from = '2025', valid_to = '2024' WHERE id = 1")
