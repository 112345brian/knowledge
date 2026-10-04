"""Issue #7: every fact carries a volatility; stable/volatile need recheck_by, static does not.

Four values in the schema: static | stable | volatile | unclassified. 'unclassified' is the LEGACY-ONLY
marker for the 295 facts that predate the column (183 of them have no recheck_by, so the issue's
"backfill everything to stable" could not satisfy its own CHECK). New facts may only carry the first
three. These tests pin the schema, every writer (add_fact, facts_batch, migrate_memory, the ingest
scripts, revisions) and the normal-db copy. Fixtures only; nothing reads the real private data.
"""
import ast
import json
import os
import sqlite3
import sys

import pytest

from test_add_fact import Env, REPO, env  # noqa: F401  (env is a fixture)
from test_fact_ingest import F, ingest, LEGACY_04  # noqa: F401  (ingest is a fixture)
from test_fact_revisions import world, entry, rec, write_log, T1, T2, T3  # noqa: F401  (world is a fixture)

sys.path.insert(0, REPO)


def schema_db():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON;")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (name) VALUES ('s')")
    return con


def raw_insert(con, volatility, recheck_by=None):
    con.execute("INSERT INTO facts (subject_id, statement, trust_level, volatility, recheck_by) VALUES (1, 'x', 'low', ?, ?)",
                (volatility, recheck_by))


# ------------------------------------------------------------------ the schema

@pytest.mark.parametrize("volatility, recheck_by", [
    ("static", None), ("static", "2030-01-01"),           # static may omit recheck_by, and may also have one
    ("stable", "2027-01-01"), ("volatile", "2027-01-01"),
    ("unclassified", None), ("unclassified", "2027-01-01"),  # legacy: exempt from the rule
])
def test_the_check_allows(volatility, recheck_by):
    con = schema_db()
    raw_insert(con, volatility, recheck_by)
    assert con.execute("SELECT volatility FROM facts").fetchone()[0] == volatility


@pytest.mark.parametrize("volatility", ["stable", "volatile"])
def test_the_check_refuses_stable_or_volatile_without_a_recheck_by(volatility):
    con = schema_db()
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(con, volatility, None)


@pytest.mark.parametrize("bad", [None, "", " ", "Static", "STABLE", "Volatile", "unknown", "static ", "legacy"])
def test_the_check_refuses_null_blank_case_variants_and_unknown_values(bad):
    con = schema_db()
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(con, bad, "2027-01-01")


def test_a_raw_insert_that_omits_volatility_fails():
    con = schema_db()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO facts (subject_id, statement, trust_level) VALUES (1, 'x', 'low')")


def test_the_check_also_guards_updates():
    con = schema_db()
    raw_insert(con, "static")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE facts SET volatility = 'stable'")  # no recheck_by on the row
    con.execute("UPDATE facts SET volatility = 'stable', recheck_by = '2027-01-01'")


def test_the_schema_comment_states_the_two_caveats_plainly():
    text = open(os.path.join(REPO, "schema.sql")).read()
    assert "NOT an excuse to default trust_level to 'verified'" in text
    assert "stable vs. volatile is self-reported guidance" in text
    assert "LEGACY ONLY" in text and "DEVIATION from #7" in text


# ------------------------------------------------------------------ add_fact

def test_cli_requires_volatility(env):
    r = env.cli("S.", "--subject", "x", "--trust", "low", volatility=None)
    assert r.returncode == 2 and "--volatility" in r.stderr and not os.path.exists(env.facts)


@pytest.mark.parametrize("bad", ["unclassified", "Static", "", "legacy"])
def test_cli_refuses_unclassified_and_other_values(env, bad):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--volatility", bad)
    assert r.returncode == 2 and not os.path.exists(env.facts)


@pytest.mark.parametrize("volatility", ["volatile", "stable"])
def test_cli_refuses_stable_or_volatile_without_recheck_by(env, volatility):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--volatility", volatility)
    assert r.returncode == 1 and "requires recheck_by" in r.stderr and not os.path.exists(env.facts)


@pytest.mark.parametrize("blank", ["", "   "])
def test_cli_treats_a_blank_recheck_by_as_missing(env, blank):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--volatility", "stable", "--recheck-by", blank)
    assert r.returncode == 1 and not os.path.exists(env.facts)


def test_cli_accepts_each_valid_combination_and_records_it(env):
    assert env.cli("A.", "--subject", "x", "--trust", "low", "--volatility", "static").returncode == 0
    assert env.cli("B.", "--subject", "x", "--trust", "low", "--volatility", "static", "--recheck-by", "2030-01-01").returncode == 0
    assert env.cli("C.", "--subject", "x", "--trust", "low", "--volatility", "stable", "--recheck-by", "2027-06-01").returncode == 0
    assert env.cli("D.", "--subject", "x", "--trust", "low", "--volatility", "volatile", "--recheck-by", "next physical").returncode == 0
    assert [(e["statement"], e["volatility"]) for e in env.entries()] == [
        ("A.", "static"), ("B.", "static"), ("C.", "stable"), ("D.", "volatile")]


def test_help_says_static_is_not_a_reason_for_verified(env):
    r = env.cli("--help", volatility=None)
    flat = " ".join(r.stdout.split())
    assert "static is NOT an excuse to default --trust to 'verified'" in flat
    assert "--volatility" in flat and "stable vs. volatile is your own guidance" in flat


def test_library_has_no_default_and_refuses_unclassified(world):
    af = world.af
    base = dict(statement="S.", subject="x", trust_level="low")
    assert any("volatility" in e for e in af.validate_fact(af.NewFact(**base), db_path="/nonexistent")[0])
    errors = af.validate_fact(af.NewFact(**base, volatility="unclassified"), db_path="/nonexistent")[0]
    assert errors and "legacy" in errors[0]
    for bad in ("", "Static", None, 3, ["static"]):
        assert af.validate_fact(af.NewFact(**base, volatility=bad), db_path="/nonexistent")[0]
    assert af.validate_fact(af.NewFact(**base, volatility="volatile"), db_path="/nonexistent")[0]
    assert af.validate_fact(af.NewFact(**base, volatility="static", recheck_by="2027-01-01"), db_path="/nonexistent")[0] == []


def test_a_refused_library_add_writes_nothing(world, tmp_path):
    p = tmp_path / "f.json"
    res = world.af.append_fact(world.af.NewFact("S.", "x", "low", volatility="volatile"), data_path=str(p), db_path="/nonexistent")
    assert not res.ok and not p.exists()


# ------------------------------------------------------------------ facts_batch and migrate_memory

@pytest.fixture
def fb(world):
    import facts_batch
    world.fb = facts_batch
    return world


def batch(w, items):
    return w.fb.add_facts(items, data_dir=w.env.data_dir, commit=False, db_path="/nonexistent")


@pytest.mark.parametrize("item, fragment", [
    ({"statement": "x", "subject": "coffee"}, "volatility is required"),
    ({"statement": "x", "subject": "coffee", "volatility": "unclassified"}, "legacy"),
    ({"statement": "x", "subject": "coffee", "volatility": "Static"}, "volatility"),
    ({"statement": "x", "subject": "coffee", "volatility": ""}, "volatility"),
    ({"statement": "x", "subject": "coffee", "volatility": "stable"}, "requires recheck_by"),
    ({"statement": "x", "subject": "coffee", "volatility": "volatile", "recheck_by": "  "}, "requires recheck_by"),
    ({"statement": "x", "subject": "coffee", "volatility": 3}, "volatility"),
])
def test_batch_rejects_a_missing_or_wrong_volatility_and_writes_nothing(fb, item, fragment):
    r = batch(fb, [{"statement": "Fine.", "subject": "coffee", "volatility": "static"}, item])
    assert not r.ok and r.items[1].outcome == "invalid"
    assert any(fragment in e for e in r.items[1].errors), r.items[1].errors
    assert r.items[0].outcome == "not_saved" and not os.path.exists(fb.env.facts)


def test_batch_accepts_each_valid_combination_and_records_it(fb):
    r = batch(fb, [{"statement": "A.", "subject": "coffee", "volatility": "static"},
                   {"statement": "B.", "subject": "coffee", "volatility": "stable", "recheck_by": "2027-01-01"},
                   {"statement": "C.", "subject": "coffee", "volatility": "volatile", "recheck_by": "2026-12-01"}])
    assert r.ok, r
    assert [e["volatility"] for e in fb.env.entries()] == ["static", "stable", "volatile"]


def test_migrate_memory_facts_are_volatile_with_a_recheck_by():
    src = open(os.path.join(REPO, "migrate_memory.py")).read()
    assert 'volatility="volatile"' in src


# ------------------------------------------------------------------ ingest: 04 (legacy files) and 11

def test_04_gives_a_legacy_entry_unclassified_and_the_predates_note_even_with_a_recheck_by(ingest):
    con = ingest.run04([F(statement="No recheck."), F(statement="Has recheck.", recheck_by="2027-01-01", notes="orig")])
    rows = {r["statement"]: r for r in con.execute("SELECT * FROM facts")}
    assert {r["volatility"] for r in rows.values()} == {"unclassified"}
    assert "predates the volatility field" in rows["No recheck."]["notes"]
    assert rows["Has recheck."]["notes"].startswith("orig\n") and "treat as stable until reviewed" in rows["Has recheck."]["notes"]
    revs = {r["statement"]: r for r in con.execute("SELECT * FROM fact_revisions")}
    assert {r["volatility"] for r in revs.values()} == {"unclassified"}
    assert "predates the volatility field" in revs["No recheck."]["notes"]
    assert rows["Has recheck."]["recheck_by"] == "2027-01-01"  # the existing recheck_by is untouched


def test_04_keeps_an_explicit_volatility_and_adds_no_note(ingest):
    con = ingest.run04([F(volatility="stable", recheck_by="2027-01-01", notes="n"), F(statement="St.", volatility="static")])
    rows = {r["statement"]: r for r in con.execute("SELECT * FROM facts")}
    assert rows["A statement."]["volatility"] == "stable" and rows["A statement."]["notes"] == "n"
    assert rows["St."]["volatility"] == "static" and rows["St."]["notes"] is None


def test_04_explicit_unclassified_is_allowed_only_in_the_legacy_files(ingest):
    con = ingest.run04([F(volatility="unclassified")])
    assert con.execute("SELECT volatility FROM facts").fetchone()[0] == "unclassified"
    with pytest.raises(Exception, match="legacy-only"):
        ingest.run04([F(volatility="unclassified", captured_via="mcp", session_id="s", source_quote="q")])
    with pytest.raises(Exception, match="legacy-only"):
        ingest.run11([F(volatility="unclassified")])


@pytest.mark.parametrize("extra", [dict(captured_via="mcp", session_id="s", source_quote="q"), dict(captured_via="cli")])
def test_04_an_entry_with_provenance_and_no_volatility_fails_loudly(ingest, extra):
    with pytest.raises(Exception, match="has no `volatility` and is not a legacy entry"):
        ingest.run04([F(**extra)])


def test_11_an_entry_without_volatility_fails_loudly_even_with_a_recheck_by(ingest):
    with pytest.raises(Exception, match=r"general_facts\.json.*has no `volatility`"):
        ingest.mod11.DATA_DIR = ingest.env.data_dir
        ingest.write("general_facts.json", ingest.stamped([F(recheck_by="2027-01-01")], "2026-09-26"))
        ingest.mod11.run(ingest.db())


@pytest.mark.parametrize("run", ["run04", "run11"])
@pytest.mark.parametrize("bad", ["Static", "", "always", 5])
def test_ingest_fails_on_an_invalid_volatility_value(ingest, run, bad):
    with pytest.raises(Exception, match="volatility"):
        getattr(ingest, run)([F(volatility=bad, recheck_by="2027-01-01")])


@pytest.mark.parametrize("run", ["run04", "run11"])
@pytest.mark.parametrize("volatility", ["stable", "volatile"])
def test_ingest_fails_on_stable_or_volatile_without_recheck_by(ingest, run, volatility):
    with pytest.raises(Exception, match="requires recheck_by"):
        getattr(ingest, run)([F(volatility=volatility)])


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_allows_static_with_and_without_a_recheck_by(ingest, run):
    con = getattr(ingest, run)([F(statement="a", volatility="static"), F(statement="b", volatility="static", recheck_by="2030-01-01")])
    assert con.execute("SELECT COUNT(*) FROM facts WHERE volatility = 'static'").fetchone()[0] == 2


def test_the_legacy_rule_does_not_apply_to_general_facts_json(ingest, monkeypatch):
    import revisions
    assert "general_facts.json" not in revisions.LEGACY_FILES
    assert set(revisions.LEGACY_FILES) == {"pilot_facts.json", "facts_batch1.json", "facts_batch2.json", "facts_batch3.json", "facts_batch4.json"}


# ------------------------------------------------------------------ revisions

def test_revision_1_carries_the_volatility_and_step_12_writes_the_latest_into_facts(world):
    world.seed(general=[entry("k1", volatility="stable", recheck_by="2027-01-01")])
    assert world.append("k1", {"volatility": "volatile", "recheck_by": "2026-12-01"}, "faster", at=T2).ok
    con = world.build()
    assert [h["volatility"] for h in world.rv.get_history(con, "k1")] == ["stable", "volatile"]
    assert con.execute("SELECT volatility, recheck_by FROM facts").fetchone()[:] == ("volatile", "2026-12-01")


def test_a_legacy_fact_has_unclassified_in_revision_1_and_a_revision_can_classify_it(world):
    world.seed(pilot=[{"subject": "a", "statement": "Old.", "trust_level": "low", "date_added": "2026-09-11"}])
    (key,) = [e["key"] for e in world.rv.load_entries(world.env.data_dir)]
    assert world.append(key, {"trust_level": "high"}, "carry over", at=T2).ok  # carries unclassified forward
    assert world.append(key, {"volatility": "stable", "recheck_by": "2027-03-01"}, "classified", at=T3).ok
    con = world.build()
    assert [h["volatility"] for h in world.rv.get_history(con, key)] == ["unclassified", "unclassified", "stable"]
    assert con.execute("SELECT volatility, recheck_by FROM facts").fetchone()[:] == ("stable", "2027-03-01")


def test_a_revision_may_set_static_with_or_without_a_recheck_by(world):
    world.seed(general=[entry("k1", volatility="stable", recheck_by="2027-01-01")])
    assert world.append("k1", {"volatility": "static"}, "never decays", at=T2).ok
    assert world.append("k1", {"recheck_by": None}, "drop it", at=T3).ok
    con = world.build()
    assert con.execute("SELECT volatility, recheck_by FROM facts").fetchone()[:] == ("static", None)


def test_a_revision_cannot_remove_the_recheck_by_of_a_stable_fact(world):
    world.seed(general=[entry("k1", volatility="stable", recheck_by="2027-01-01")])
    r = world.append("k1", {"recheck_by": None}, "oops", at=T2)
    assert not r.ok and "requires recheck_by" in r.errors[0] and not os.path.exists(world.log)
    r = world.append("k1", {"recheck_by": "   "}, "oops", at=T2)
    assert not r.ok and not os.path.exists(world.log)


def test_a_revision_cannot_turn_a_static_fact_without_recheck_by_into_stable_or_volatile(world):
    world.seed(general=[entry("k1")])
    for v in ("stable", "volatile"):
        r = world.append("k1", {"volatility": v}, "x", at=T2)
        assert not r.ok and "requires recheck_by" in r.errors[0]
    assert world.append("k1", {"volatility": "volatile", "recheck_by": "2027-01-01"}, "ok", at=T2).ok


@pytest.mark.parametrize("bad", ["unclassified", "Static", "", None, "always"])
def test_a_revision_cannot_set_an_invalid_volatility_and_unclassified_is_not_choosable(world, bad):
    world.seed(general=[entry("k1", volatility="stable", recheck_by="2027-01-01")])
    r = world.append("k1", {"volatility": bad}, "x", at=T2)
    assert not r.ok and not os.path.exists(world.log)


def test_unclassified_cannot_be_reintroduced_after_classification(world):
    world.seed(pilot=[{"subject": "a", "statement": "Old.", "trust_level": "low", "date_added": "2026-09-11"}])
    (key,) = [e["key"] for e in world.rv.load_entries(world.env.data_dir)]
    assert world.append(key, {"volatility": "static"}, "classified", at=T2).ok
    r = world.append(key, {"volatility": "unclassified"}, "back", at=T3)
    assert not r.ok and "legacy-only" in r.errors[0]


def test_the_build_rejects_hand_written_log_lines_the_check_would_refuse(world):
    world.seed(general=[entry("k1")])
    for line, pattern in [
        (rec(volatility="stable"), r":1: .*requires recheck_by"),                # stable without recheck_by
        (rec(volatility=None), r":1: .*volatility"),                            # a pre-#7 line with a null volatility
        (rec(volatility="Static"), r":1: .*volatility"),
        (rec(volatility="unclassified"), r":1: .*legacy-only"),                  # k1 was never unclassified
    ]:
        write_log(world, line)
        with pytest.raises(world.rv.RevisionError, match=pattern):
            world.build()


def test_the_build_accepts_a_valid_hand_written_line(world):
    world.seed(general=[entry("k1")])
    write_log(world, rec(volatility="volatile", recheck_by="2027-01-01"))
    con = world.build()
    assert con.execute("SELECT volatility FROM facts").fetchone()[0] == "volatile"


# ------------------------------------------------------------------ normal db

def test_normal_db_copies_volatility_with_the_same_check():
    import leak_test
    import normal_db
    import tempfile
    import privacy
    with tempfile.TemporaryDirectory() as tmp:
        full = leak_test.build_fixture(tmp)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        path, _ = normal_db.build_normal_atomic(full, out, privacy.Rules())
        con = sqlite3.connect(path)
        assert "volatility" in {r[1] for r in con.execute("PRAGMA table_info(facts)")}
        assert {r[0] for r in con.execute("SELECT volatility FROM facts")} == {"static"}
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE facts SET volatility = 'stable'")  # no recheck_by on these rows


# ------------------------------------------------------------------ callers must supply it

def _py_sources():
    for name in sorted(os.listdir(REPO)):
        if name.endswith(".py"):
            yield name, os.path.join(REPO, name)


def test_every_production_NewFact_call_names_a_volatility():
    """A caller that builds a NewFact without one is a validation error at runtime; catch it here."""
    offenders = []
    for name, path in _py_sources():
        for node in ast.walk(ast.parse(open(path).read())):
            if isinstance(node, ast.Call):
                f = node.func
                called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if called == "NewFact" and not any(k.arg == "volatility" or k.arg is None for k in node.keywords):
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"NewFact(...) without volatility=: {offenders}"


def test_every_production_facts_insert_names_the_volatility_column():
    offenders = []
    for name, path in _py_sources():
        if name == "normal_db.py":  # builds its column list in a variable; test_normal_db_copies_volatility covers it
            continue
        text = open(path).read()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "INSERT INTO facts (" in node.value:
                if "volatility" not in node.value:
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"INSERT INTO facts without the volatility column: {offenders}"
