"""Issue #7: every fact needs a recheck_by, or an explicit 'does not decay' assertion with a written rationale.

Three values in the schema: recheck | no-decay | unreviewed. `recheck` requires recheck_by; `no-decay`
requires a non-blank recheck_rationale (recheck_by may be NULL); `unreviewed` is the LEGACY-ONLY marker
for the facts that predate the column and have no recheck_by (183 of the 295). New facts may only
carry the first two, and freshness is DERIVED by the writers, never passed in. These tests pin the
schema, every writer (add_fact, facts_batch, migrate_memory, the ingest scripts, revisions) and the
normal-db copy. Fixtures only; nothing reads the real private data.
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


def raw_insert(con, freshness, recheck_by=None, rationale=None):
    con.execute("INSERT INTO facts (subject_id, statement, trust_level, freshness, recheck_by, recheck_rationale) "
                "VALUES (1, 'x', 'low', ?, ?, ?)", (freshness, recheck_by, rationale))


# ------------------------------------------------------------------ the schema

@pytest.mark.parametrize("freshness, recheck_by, rationale", [
    ("recheck", "2027-01-01", None), ("recheck", "2027-01-01", "why"),
    ("no-decay", None, "a birthdate does not change"),            # recheck_by may be NULL
    ("no-decay", "2030-01-01", "does not decay"),                   # and may also be present
    ("unreviewed", None, None), ("unreviewed", "2027-01-01", None),  # legacy: exempt
])
def test_the_check_allows(freshness, recheck_by, rationale):
    con = schema_db()
    raw_insert(con, freshness, recheck_by, rationale)
    assert con.execute("SELECT freshness FROM facts").fetchone()[0] == freshness


def test_the_check_refuses_recheck_without_a_recheck_by():
    con = schema_db()
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(con, "recheck", None, "a rationale does not help a recheck fact")


@pytest.mark.parametrize("rationale", [None, "", " ", "  \t\n"])
def test_the_check_refuses_no_decay_without_a_non_blank_rationale(rationale):
    con = schema_db()
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(con, "no-decay", "2027-01-01", rationale)  # a recheck_by does not substitute


@pytest.mark.parametrize("bad", [None, "", " ", "Recheck", "NO-DECAY", "No-Decay", "static", "stable", "volatile", "unclassified",
                                 "recheck ", "legacy", "no_decay"])
def test_the_check_refuses_null_blank_case_variants_and_unknown_values(bad):
    con = schema_db()
    with pytest.raises(sqlite3.IntegrityError):
        raw_insert(con, bad, "2027-01-01", "why")


def test_a_raw_insert_that_omits_freshness_fails():
    con = schema_db()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO facts (subject_id, statement, trust_level) VALUES (1, 'x', 'low')")


def test_the_check_also_guards_updates():
    con = schema_db()
    raw_insert(con, "unreviewed")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE facts SET freshness = 'recheck'")  # no recheck_by on the row
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE facts SET freshness = 'no-decay', recheck_rationale = '  '")
    con.execute("UPDATE facts SET freshness = 'recheck', recheck_by = '2027-01-01'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE facts SET recheck_by = NULL")  # clearing it from a recheck fact


def test_the_vocabulary_is_gone_and_the_schema_comment_states_the_caveats_plainly():
    text = open(os.path.join(REPO, "schema.sql")).read()
    assert "NOT an excuse to default trust_level to 'verified'" in text
    assert "cannot judge whether a fact really does not decay" in text
    assert "LEGACY ONLY" in text


def test_no_trace_of_the_old_volatility_scale_in_the_repo():
    # ('unclassified' used to be banned here as the old volatility value; #39 reuses the word as a fact kind.)
    banned = ("volatil", "stable vs", "static vs")
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__", ".pytest_cache", ".claude", ".tach", "node_modules")]
        for name in files:
            if not name.endswith((".py", ".sql", ".md", ".toml", ".json")) or name in ("test_freshness.py", "uv.lock"):
                continue
            text = open(os.path.join(root, name), errors="replace").read().lower()
            if any(b in text for b in banned):
                offenders.append(os.path.relpath(os.path.join(root, name), REPO))
    assert offenders == []


# ------------------------------------------------------------------ add_fact

def test_cli_refuses_neither_recheck_by_nor_no_decay(env):
    r = env.cli("S.", "--subject", "x", "--trust", "low", freshness=False)
    assert r.returncode == 1 and "needs freshness" in r.stderr and not os.path.exists(env.facts)


def test_cli_has_no_volatility_flag(env):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--volatility", "static", freshness=False)
    assert r.returncode == 2 and not os.path.exists(env.facts)


def test_cli_refuses_both_recheck_by_and_no_decay(env):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--recheck-by", "2027-01-01", "--no-decay",
                "--recheck-rationale", "does not decay", freshness=False)
    assert r.returncode == 1 and "contradict" in r.stderr and not os.path.exists(env.facts)


@pytest.mark.parametrize("rationale", [None, "", "   "])
def test_cli_refuses_no_decay_without_a_non_blank_rationale(env, rationale):
    extra = [] if rationale is None else ["--recheck-rationale", rationale]
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--no-decay", *extra, freshness=False)
    assert r.returncode == 1 and "non-blank recheck_rationale" in r.stderr and not os.path.exists(env.facts)


@pytest.mark.parametrize("blank", ["", "   "])
def test_cli_treats_a_blank_recheck_by_as_missing(env, blank):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--recheck-by", blank, freshness=False)
    assert r.returncode == 1 and not os.path.exists(env.facts)


def test_cli_accepts_each_valid_combination_and_records_it(env):
    assert env.cli("A.", "--subject", "x", "--trust", "low", "--no-decay", "--recheck-rationale", "a birthdate", freshness=False).returncode == 0
    assert env.cli("C.", "--subject", "x", "--trust", "low", "--recheck-by", "2027-06-01", freshness=False).returncode == 0
    assert env.cli("D.", "--subject", "x", "--trust", "low", "--recheck-by", "next physical",
                   "--recheck-rationale", "yearly", freshness=False).returncode == 0
    got = [(e["statement"], e["freshness"], e.get("recheck_by"), e.get("recheck_rationale")) for e in env.entries()]
    assert got == [("A.", "no-decay", None, "a birthdate"), ("C.", "recheck", "2027-06-01", None),
                   ("D.", "recheck", "next physical", "yearly")]


def test_help_says_no_decay_is_not_a_reason_for_verified(env):
    r = env.cli("--help", freshness=False)
    flat = " ".join(r.stdout.split())
    assert "NOT an excuse to default --trust to 'verified'" in flat
    assert "--no-decay" in flat and "--volatility" not in flat
    assert "cannot judge whether a fact really does not decay" in flat


def test_library_derives_freshness_and_refuses_the_edge_inputs(world):
    af = world.af
    base = dict(statement="S.", subject="x", trust_level="low")

    def errs(**kw):
        return af.validate_fact(af.NewFact(**base, **kw), db_path="/nonexistent")[0]

    assert errs(recheck_by="2027-01-01") == []
    assert errs(no_decay=True, recheck_rationale="does not decay") == []
    assert errs(no_decay=True, recheck_rationale="does not decay", recheck_by="  ") == []  # a blank recheck_by is absent
    assert errs()                                                                         # neither
    assert errs(recheck_by="   ")                                                         # blank counts as missing
    assert any("contradict" in e for e in errs(recheck_by="2027-01-01", no_decay=True, recheck_rationale="r"))
    for bad in (None, "", "   "):
        assert any("non-blank recheck_rationale" in e for e in errs(no_decay=True, recheck_rationale=bad))
    for bad in ("yes", 1, None, [True]):
        assert errs(no_decay=bad, recheck_by="2027-01-01")
    # a caller cannot supply freshness at all, so cannot supply 'unreviewed'
    with pytest.raises(TypeError):
        af.NewFact(**base, freshness="unreviewed", recheck_by="2027-01-01")
    with pytest.raises(TypeError):
        af.NewFact(**base, volatility="static")


def test_the_entry_stores_the_derived_freshness_explicitly(world):
    af = world.af
    e1 = af.build_entry(af.NewFact("S.", "x", "low", recheck_by="2027-01-01"))
    e2 = af.build_entry(af.NewFact("S.", "x", "low", no_decay=True, recheck_rationale="r", recheck_by="  "))
    assert (e1["freshness"], e1["recheck_by"]) == ("recheck", "2027-01-01")
    assert (e2["freshness"], e2["recheck_rationale"], "recheck_by" in e2) == ("no-decay", "r", False)


def test_a_refused_library_add_writes_nothing(world, tmp_path):
    p = tmp_path / "f.json"
    res = world.af.append_fact(world.af.NewFact("S.", "x", "low"), data_path=str(p), db_path="/nonexistent")
    assert not res.ok and not p.exists()
    res = world.af.append_fact(world.af.NewFact("S.", "x", "low", no_decay=True, recheck_by="2027-01-01", recheck_rationale="r"),
                               data_path=str(p), db_path="/nonexistent")
    assert not res.ok and not p.exists()


# ------------------------------------------------------------------ facts_batch and migrate_memory

@pytest.fixture
def fb(world):
    import facts_batch
    world.fb = facts_batch
    return world


def batch(w, items):
    return w.fb.add_facts(items, data_dir=w.env.data_dir, commit=False, db_path="/nonexistent")


GOOD = {"statement": "Fine.", "subject": "coffee", "recheck_by": "2027-01-01"}


@pytest.mark.parametrize("item, fragment", [
    ({"statement": "x", "subject": "coffee"}, "needs freshness"),
    ({"statement": "x", "subject": "coffee", "recheck_by": "  "}, "needs freshness"),
    ({"statement": "x", "subject": "coffee", "no_decay": False}, "needs freshness"),
    ({"statement": "x", "subject": "coffee", "no_decay": True}, "non-blank recheck_rationale"),
    ({"statement": "x", "subject": "coffee", "no_decay": True, "recheck_rationale": "   "}, "non-blank recheck_rationale"),
    ({"statement": "x", "subject": "coffee", "no_decay": True, "recheck_rationale": "r", "recheck_by": "2027-01-01"}, "contradict"),
    ({"statement": "x", "subject": "coffee", "no_decay": "yes", "recheck_rationale": "r"}, "no_decay"),
    ({"statement": "x", "subject": "coffee", "freshness": "unreviewed", "recheck_by": "2027-01-01"}, "unknown key"),
    ({"statement": "x", "subject": "coffee", "volatility": "static"}, "unknown key"),
])
def test_batch_rejects_bad_freshness_and_writes_nothing(fb, item, fragment):
    r = batch(fb, [dict(GOOD), item])
    assert not r.ok and r.items[1].outcome == "invalid"
    assert any(fragment in e for e in r.items[1].errors), r.items[1].errors
    assert r.items[0].outcome == "not_saved" and not os.path.exists(fb.env.facts)


def test_batch_accepts_each_valid_combination_and_records_it(fb):
    r = batch(fb, [{"statement": "A.", "subject": "coffee", "no_decay": True, "recheck_rationale": "a birthdate"},
                   {"statement": "B.", "subject": "coffee", "recheck_by": "2027-01-01"},
                   {"statement": "C.", "subject": "coffee", "recheck_by": "next physical", "recheck_rationale": "yearly"}])
    assert r.ok, r
    assert [e["freshness"] for e in fb.env.entries()] == ["no-decay", "recheck", "recheck"]


def test_migrate_memory_facts_are_recheck_with_a_recheck_by():
    # the mapping to a NewFact lives in the domain module; the use case must not set freshness itself
    src = open(os.path.join(REPO, "migrate_memory_rules.py")).read()
    assert "recheck_by=recheck" in src and "no_decay" not in src
    assert "no_decay" not in open(os.path.join(REPO, "migrate_memory.py")).read()


# ------------------------------------------------------------------ ingest: 04 (legacy files) and 11

def test_04_legacy_entry_with_a_recheck_by_is_recheck_and_without_is_unreviewed_with_the_note(ingest):
    con = ingest.run04([F(statement="No recheck."), F(statement="Has recheck.", recheck_by="2027-01-01", notes="orig"),
                        F(statement="Blank recheck.", recheck_by="  ")])
    rows = {r["statement"]: r for r in con.execute("SELECT * FROM facts")}
    assert rows["No recheck."]["freshness"] == "unreviewed"
    assert rows["Blank recheck."]["freshness"] == "unreviewed"
    assert rows["Has recheck."]["freshness"] == "recheck"
    assert "predates the freshness field" in rows["No recheck."]["notes"]
    assert rows["Has recheck."]["notes"] == "orig"            # no note, and the existing notes untouched
    assert rows["Has recheck."]["recheck_by"] == "2027-01-01"
    revs = {r["statement"]: r for r in con.execute("SELECT * FROM fact_revisions")}
    assert revs["No recheck."]["freshness"] == "unreviewed" and "predates the freshness field" in revs["No recheck."]["notes"]
    assert revs["Has recheck."]["freshness"] == "recheck" and revs["Has recheck."]["notes"] == "orig"


def test_04_keeps_an_explicit_freshness_and_adds_no_note(ingest):
    con = ingest.run04([F(freshness="recheck", recheck_by="2027-01-01", notes="n"),
                        F(statement="St.", freshness="no-decay", recheck_rationale="does not decay")])
    rows = {r["statement"]: r for r in con.execute("SELECT * FROM facts")}
    assert rows["A statement."]["freshness"] == "recheck" and rows["A statement."]["notes"] == "n"
    assert rows["St."]["freshness"] == "no-decay" and rows["St."]["notes"] is None


def test_04_explicit_unreviewed_is_allowed_only_in_the_legacy_files(ingest):
    con = ingest.run04([F(freshness="unreviewed")])
    assert con.execute("SELECT freshness FROM facts").fetchone()[0] == "unreviewed"
    with pytest.raises(Exception, match="legacy-only"):
        ingest.run04([F(freshness="unreviewed", captured_via="mcp", session_id="s", source_quote="q")])
    with pytest.raises(Exception, match="legacy-only"):
        ingest.run11([F(freshness="unreviewed")])


@pytest.mark.parametrize("extra", [dict(captured_via="mcp", session_id="s", source_quote="q"), dict(captured_via="cli")])
def test_04_a_new_style_entry_without_freshness_fails_loudly_even_with_a_recheck_by(ingest, extra):
    with pytest.raises(Exception, match=r"pilot_facts\.json.*has no `freshness` and is not a legacy entry"):
        ingest.run04([F(recheck_by="2027-01-01", **extra)])


def test_11_an_entry_without_freshness_fails_loudly_even_with_a_recheck_by(ingest):
    with pytest.raises(Exception, match=r"general_facts\.json.*has no `freshness`"):
        ingest.mod11.DATA_DIR = ingest.env.data_dir
        ingest.write("general_facts.json", ingest.stamped([F(recheck_by="2027-01-01")], "2026-09-26"))
        ingest.mod11.run(ingest.db())


@pytest.mark.parametrize("run", ["run04", "run11"])
@pytest.mark.parametrize("bad", ["Recheck", "NO-DECAY", "", "always", 5, "static", "unclassified"])
def test_ingest_fails_on_an_invalid_freshness_value(ingest, run, bad):
    with pytest.raises(Exception, match="freshness"):
        getattr(ingest, run)([F(freshness=bad, recheck_by="2027-01-01", recheck_rationale="r")])


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_fails_on_recheck_without_recheck_by_and_no_decay_without_rationale(ingest, run):
    with pytest.raises(Exception, match="requires recheck_by"):
        getattr(ingest, run)([F(freshness="recheck", recheck_rationale=None)])
    for blank in (None, "", "  "):
        with pytest.raises(Exception, match="non-blank recheck_rationale"):
            getattr(ingest, run)([F(freshness="no-decay", recheck_rationale=blank, recheck_by="2027-01-01")])


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_allows_no_decay_with_and_without_a_recheck_by(ingest, run):
    con = getattr(ingest, run)([F(statement="a", freshness="no-decay", recheck_rationale="r"),
                                F(statement="b", freshness="no-decay", recheck_rationale="r", recheck_by="2030-01-01")])
    assert con.execute("SELECT COUNT(*) FROM facts WHERE freshness = 'no-decay'").fetchone()[0] == 2


def test_the_legacy_rule_does_not_apply_to_general_facts_json(ingest, monkeypatch):
    import revisions
    assert "general_facts.json" not in revisions.LEGACY_FILES
    assert set(revisions.LEGACY_FILES) == {"pilot_facts.json", "facts_batch1.json", "facts_batch2.json", "facts_batch3.json", "facts_batch4.json"}


# ------------------------------------------------------------------ revisions

def test_revision_1_carries_the_freshness_and_step_12_writes_the_latest_into_facts(world):
    world.seed(general=[entry("k1", freshness="recheck", recheck_by="2027-01-01")])
    assert world.append("k1", {"freshness": "no-decay", "recheck_rationale": "does not decay"}, "reassessed", at=T2).ok
    con = world.build()
    assert [h["freshness"] for h in world.rs.get_history(con, "k1")] == ["recheck", "no-decay"]
    assert con.execute("SELECT freshness, recheck_rationale FROM facts").fetchone()[:] == ("no-decay", "does not decay")


def test_a_legacy_fact_is_unreviewed_in_revision_1_and_a_revision_can_review_it(world):
    world.seed(pilot=[{"subject": "a", "statement": "Old.", "trust_level": "low", "date_added": "2026-09-11"}])
    (key,) = [e["key"] for e in world.rs.load_entries(world.env.data_dir)]
    assert world.append(key, {"trust_level": "high"}, "carry over", at=T2).ok  # carries unreviewed forward
    assert world.append(key, {"freshness": "recheck", "recheck_by": "2027-03-01"}, "reviewed", at=T3).ok
    con = world.build()
    assert [h["freshness"] for h in world.rs.get_history(con, key)] == ["unreviewed", "unreviewed", "recheck"]
    assert con.execute("SELECT freshness, recheck_by FROM facts").fetchone()[:] == ("recheck", "2027-03-01")


def test_a_legacy_fact_can_be_reviewed_straight_to_no_decay(world):
    world.seed(pilot=[{"subject": "a", "statement": "Old.", "trust_level": "low", "date_added": "2026-09-11"}])
    (key,) = [e["key"] for e in world.rs.load_entries(world.env.data_dir)]
    assert not world.append(key, {"freshness": "no-decay"}, "no rationale", at=T2).ok
    assert world.append(key, {"freshness": "no-decay", "recheck_rationale": "completed purchase"}, "reviewed", at=T2).ok
    con = world.build()
    assert con.execute("SELECT freshness, recheck_by FROM facts").fetchone()[:] == ("no-decay", None)


def test_a_revision_may_move_between_recheck_and_no_decay_while_the_checks_hold(world):
    world.seed(general=[entry("k1", freshness="recheck", recheck_by="2027-01-01", recheck_rationale=None)])
    assert world.append("k1", {"freshness": "no-decay", "recheck_rationale": "never decays", "recheck_by": None}, "x", at=T2).ok
    assert world.append("k1", {"freshness": "recheck", "recheck_by": "2028-01-01"}, "y", at=T3).ok
    con = world.build()
    assert con.execute("SELECT freshness, recheck_by FROM facts").fetchone()[:] == ("recheck", "2028-01-01")


def test_a_revision_cannot_clear_the_recheck_by_of_a_recheck_fact(world):
    world.seed(general=[entry("k1", freshness="recheck", recheck_by="2027-01-01")])
    r = world.append("k1", {"recheck_by": None}, "oops", at=T2)
    assert not r.ok and "requires recheck_by" in r.errors[0] and not os.path.exists(world.log)
    r = world.append("k1", {"recheck_by": "   "}, "oops", at=T2)
    assert not r.ok and not os.path.exists(world.log)


def test_a_revision_cannot_blank_the_rationale_of_a_no_decay_fact(world):
    world.seed(general=[entry("k1")])
    for blank in (None, "", "  "):
        r = world.append("k1", {"recheck_rationale": blank}, "oops", at=T2)
        assert not r.ok and "non-blank recheck_rationale" in r.errors[0] and not os.path.exists(world.log)


def test_a_revision_cannot_turn_a_no_decay_fact_into_recheck_without_a_recheck_by(world):
    world.seed(general=[entry("k1")])
    r = world.append("k1", {"freshness": "recheck"}, "x", at=T2)
    assert not r.ok and "requires recheck_by" in r.errors[0]
    assert world.append("k1", {"freshness": "recheck", "recheck_by": "2027-01-01"}, "ok", at=T2).ok


@pytest.mark.parametrize("bad", ["unreviewed", "Recheck", "NO-DECAY", "", None, "always", "static"])
def test_a_revision_cannot_set_an_invalid_freshness_and_unreviewed_is_not_choosable(world, bad):
    world.seed(general=[entry("k1", freshness="recheck", recheck_by="2027-01-01")])
    r = world.append("k1", {"freshness": bad}, "x", at=T2)
    assert not r.ok and not os.path.exists(world.log)


def test_unreviewed_cannot_be_reintroduced_after_review(world):
    world.seed(pilot=[{"subject": "a", "statement": "Old.", "trust_level": "low", "date_added": "2026-09-11"}])
    (key,) = [e["key"] for e in world.rs.load_entries(world.env.data_dir)]
    assert world.append(key, {"freshness": "recheck", "recheck_by": "2027-01-01"}, "reviewed", at=T2).ok
    r = world.append(key, {"freshness": "unreviewed"}, "back", at=T3)
    assert not r.ok and "unreviewed" in r.errors[0]


def test_the_build_rejects_hand_written_log_lines_the_checks_would_refuse(world):
    world.seed(general=[entry("k1")])
    for line, pattern in [
        (rec(freshness="recheck"), r":1: .*requires recheck_by"),                     # recheck without recheck_by
        (rec(freshness="no-decay", recheck_rationale="  "), r":1: .*non-blank recheck_rationale"),
        (rec(freshness=None), r":1: .*freshness"),                                    # a pre-freshness line
        (rec(freshness="No-Decay"), r":1: .*freshness"),
        (rec(freshness="unreviewed"), r":1: .*unreviewed"),                           # k1 was never unreviewed
    ]:
        write_log(world, line)
        with pytest.raises(world.rv.RevisionError, match=pattern):
            world.build()


def test_the_build_accepts_a_valid_hand_written_line(world):
    world.seed(general=[entry("k1")])
    write_log(world, rec(freshness="recheck", recheck_by="2027-01-01"))
    con = world.build()
    assert con.execute("SELECT freshness FROM facts").fetchone()[0] == "recheck"


# ------------------------------------------------------------------ normal db

def test_normal_db_copies_freshness_with_the_same_checks():
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
        assert "freshness" in {r[1] for r in con.execute("PRAGMA table_info(facts)")}
        assert {r[0] for r in con.execute("SELECT freshness FROM facts")} == {"unreviewed"}
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE facts SET freshness = 'recheck'")  # no recheck_by on these rows
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE facts SET freshness = 'no-decay'")  # no rationale either


# ------------------------------------------------------------------ callers must supply it

def _py_sources():
    for name in sorted(os.listdir(REPO)):
        if name.endswith(".py"):
            yield name, os.path.join(REPO, name)


def test_every_production_NewFact_call_names_its_freshness_inputs():
    """A caller that builds a NewFact with neither recheck_by nor no_decay is a validation error at runtime; catch it here."""
    offenders = []
    for name, path in _py_sources():
        for node in ast.walk(ast.parse(open(path).read())):
            if isinstance(node, ast.Call):
                f = node.func
                called = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if called == "NewFact" and not any(k.arg in ("recheck_by", "no_decay") or k.arg is None for k in node.keywords):
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"NewFact(...) without recheck_by= or no_decay=: {offenders}"


def test_every_production_facts_insert_names_the_freshness_column():
    offenders = []
    for name, path in _py_sources():
        if name == "normal_db.py":  # builds its column list in a variable; test_normal_db_copies_freshness covers it
            continue
        text = open(path).read()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "INSERT INTO facts (" in node.value:
                if "freshness" not in node.value:
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"INSERT INTO facts without the freshness column: {offenders}"
