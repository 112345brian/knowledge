"""#45: facts.applies_to, who or what a fact applies to (short free text, never inferred).

NULL means unknown and is distinct from an explicit "general"; blank/whitespace normalizes to NULL;
it is a mutable revision field, part of the facts FTS, shown by show/search, privacy-scanned like the other
free-text fields, and included in the normal-only DB and the modes row shapes.
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

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


def nf(**kw):
    import add_fact
    base = dict(statement="S.", subject="x", trust_level="low", no_decay=True, recheck_rationale="r")
    base.update(kw)
    return add_fact.NewFact(**base)


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (id, name, domain) VALUES (1, 's', 'g')")
    return con


# ------------------------------------------------------------------ writers

def test_add_fact_stores_stripped_text_and_omits_blank_and_none():
    import add_fact
    assert add_fact.build_entry(nf(applies_to="  adult men, 18-35  "))["applies_to"] == "adult men, 18-35"
    for blank in (None, "", "   ", "\n\t "):
        assert "applies_to" not in add_fact.build_entry(nf(applies_to=blank)), repr(blank)
    assert add_fact.validate_fact(nf(applies_to="general"), db_path="/nonexistent")[0] == []
    assert add_fact.validate_fact(nf(applies_to="  "), db_path="/nonexistent")[0] == []     # blank is fine: it becomes absent
    assert add_fact.validate_fact(nf(applies_to=5), db_path="/nonexistent")[0]


def test_explicit_general_is_kept_and_distinct_from_absent():
    import add_fact
    assert add_fact.build_entry(nf(applies_to="general"))["applies_to"] == "general"
    assert "applies_to" not in add_fact.build_entry(nf())


def test_cli_flag_round_trips(tmp_path):
    from test_add_fact import Env
    env = Env(tmp_path)
    assert env.cli("Dose.", "--subject", "x", "--trust", "low", "--applies-to", "adults with T2D").returncode == 0
    assert env.entries()[0]["applies_to"] == "adults with T2D"
    assert env.cli("Other.", "--subject", "x", "--trust", "low", "--applies-to", "   ").returncode == 0
    assert "applies_to" not in env.entries()[1]


def test_batch_items_take_applies_to():
    import facts_batch
    assert "applies_to" in facts_batch.ITEM_KEYS


# ------------------------------------------------------------------ schema + ingest

def test_schema_check_rejects_blank_but_not_null_or_general():
    con = _con()
    ins = ("INSERT INTO facts (subject_id, statement, trust_level, freshness, applies_to) "
           "VALUES (1, 'a', 'low', 'unreviewed', ?)")
    for v in (None, "general", "adult men"):
        con.execute(ins, (v,))
    for bad in ("", "   ", "\n"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(ins, (bad,))
    assert con.execute("SELECT COUNT(*) FROM facts WHERE applies_to IS NULL").fetchone()[0] == 1


def test_ingest_normalizes_and_keeps_legacy_null(ingest):
    con = ingest.run04([F(applies_to="  young men  "), F(statement="blank.", applies_to="   "), F(statement="none.")])
    assert [r[0] for r in con.execute("SELECT applies_to FROM facts ORDER BY id")] == ["young men", None, None]
    assert [r[0] for r in con.execute("SELECT applies_to FROM fact_revisions ORDER BY id")] == ["young men", None, None]
    con = ingest.run11([F(applies_to="general")])
    assert con.execute("SELECT applies_to FROM facts").fetchone()[0] == "general"


def test_ingest_fails_loudly_on_non_text(ingest):
    for run in (ingest.run04, ingest.run11):
        with pytest.raises(Exception, match="applies_to") as e:
            run([F(applies_to=["adults"])])
        assert type(e.value).__name__ == "RevisionError"


# ------------------------------------------------------------------ revisions

def test_clean_applies_to():
    import revisions
    assert revisions.clean_applies_to(None) is None
    assert revisions.clean_applies_to("  x ") == "x"
    assert revisions.clean_applies_to(" \n ") is None
    with pytest.raises(ValueError):
        revisions.clean_applies_to(3)


def test_revision_record_shape_for_applies_to():
    import revisions
    snap = revisions.entry_snapshot({"statement": "x", "trust_level": "low", "freshness": "no-decay",
                                     "recheck_rationale": "r", "applies_to": " men "})
    assert snap["applies_to"] == "men"
    rec = {"source_key": "k-one", "revision": 2, "changed_at": "2026-02-01T00:00:00+00:00", "changed_via": "cli",
           "session_id": None, "change_reason": "narrowed", **snap}
    rec = {k: rec[k] for k in revisions.REVISION_KEYS}
    assert revisions.validate_record_shape(rec) == []
    assert revisions.validate_record_shape(dict(rec, applies_to=None)) == []
    assert revisions.validate_record_shape(dict(rec, applies_to="   "))
    assert revisions.validate_record_shape(dict(rec, applies_to=3))


def test_append_revision_normalizes_blank_to_null_and_round_trips_through_as_of(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    env.sql("UPDATE fact_revisions SET applies_to = NULL WHERE source_key = 'k-one' AND revision = 1")
    env.sql("UPDATE fact_revisions SET applies_to = 'adult men' WHERE source_key = 'k-one' AND revision = 2")
    env.sql("UPDATE facts SET applies_to = 'adult men' WHERE id = 1")
    assert "Applies to" not in env.cli("show", "1", "--as-of", "2026-03-01").stdout
    assert "Applies to: adult men" in env.cli("show", "1", "--as-of", "2026-07-01").stdout
    assert "Applies to: adult men" in env.cli("show", "1").stdout
    assert "Applies to" not in env.cli("show", "2").stdout


def test_append_revision_library_cleans_the_value(tmp_path, monkeypatch):
    import revisions_store
    # the real append path: a log file in a throwaway data dir with one entry
    import revisions
    d = tmp_path / "data"
    d.mkdir()
    (d / "general_facts.json").write_text(json.dumps([{
        "source_key": "k1", "subject": "s", "statement": "Fact.", "trust_level": "low", "date_added": "2026-01-01T00:00:00+00:00",
        "freshness": "no-decay", "recheck_rationale": "r", "status": "active", "visibility": "private"}]))
    r = revisions_store.append_revision("k1", {"applies_to": "  adults  "}, "scoped", "cli", data_dir=str(d))
    assert r.ok, r.errors
    assert r.revision["applies_to"] == "adults"
    r = revisions_store.append_revision("k1", {"applies_to": "   "}, "cleared", "cli", data_dir=str(d))
    assert r.ok and r.revision["applies_to"] is None
    assert not revisions_store.append_revision("k1", {"applies_to": "   "}, "again", "cli", data_dir=str(d)).ok      # already null: no change
    assert not revisions_store.append_revision("k1", {"applies_to": 5}, "bad", "cli", data_dir=str(d)).ok


# ------------------------------------------------------------------ search, FTS, rows

def _facts_db():
    con = _con()
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, applies_to, visibility, source_key) "
                "VALUES (1, 1, 'Creatine helps.', 'low', 'unreviewed', 'example population', 'normal', 'k1')")
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, visibility, source_key) "
                "VALUES (2, 1, 'Creatine is safe.', 'low', 'unreviewed', 'normal', 'k2')")
    return con


def test_fts_indexes_applies_to_and_follows_updates():
    import knowledge
    con = _facts_db()
    assert [r["id"] for r in knowledge.search_facts(con, "example population")] == [1]
    assert [r["id"] for r in knowledge.search_facts(con, "creatine")] == [1, 2] or {r["id"] for r in knowledge.search_facts(con, "creatine")} == {1, 2}
    con.execute("UPDATE facts SET applies_to = 'teenagers' WHERE id = 1")
    assert knowledge.search_facts(con, "example population") == []
    assert [r["id"] for r in knowledge.search_facts(con, "teenagers")] == [1]
    con.execute("DELETE FROM facts WHERE id = 1")
    assert knowledge.search_facts(con, "teenagers") == []


def test_rows_carry_applies_to_in_both_layers():
    import knowledge
    import modes
    import modes_store
    con = _facts_db()
    rows = {r["id"]: r for r in knowledge.list_facts(con)}
    assert rows[1]["applies_to"] == "example population" and rows[2]["applies_to"] is None
    assert knowledge.get_fact(con, 1)["applies_to"] == "example population"
    s = modes.Session(mode=modes.Mode.normal)
    assert {r["id"]: r["applies_to"] for r in modes_store.list_facts(s, con)} == {1: "example population", 2: None}
    assert modes_store.search_facts(s, con, "example population")[0]["id"] == 1
    assert modes_store.get_fact(s, con, 1)["applies_to"] == "example population"


def test_cli_search_and_facts_show_applies_to(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    env.sql("UPDATE facts SET applies_to = 'zebrafinch keepers' WHERE id = 1")
    out = env.cli("facts").stdout
    assert "(applies to: zebrafinch keepers)" in out
    assert [r["id"] for r in json.loads(env.cli("search", "zebrafinch", "--json").stdout)] == [1]
    assert json.loads(env.cli("facts", "--json").stdout)[0]["applies_to"] == "zebrafinch keepers"
    assert "applies to" not in env.cli("facts", "--subject", "nutrition").stdout


# ------------------------------------------------------------------ privacy + normal-only DB

def test_a_rule_keyword_inside_applies_to_makes_the_fact_private():
    import privacy
    import privacy_store
    rules = privacy.Rules(keywords=("zorbak",))
    res = privacy.resolve_visibility("s", "Innocent statement.", "normal", rules, extra_text=(None, "applies to Zorbak relatives"))
    assert res.visibility == "private"
    con = _con()
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, applies_to, visibility, source_key) "
                "VALUES (1, 1, 'Innocent statement.', 'low', 'unreviewed', 'Zorbak relatives', 'normal', 'k1')")
    raised = privacy_store.apply_rules_to_db(con, rules)["raised"]
    assert [r[0] for r in raised] == [1]
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "private"


def test_add_fact_resolution_scans_applies_to(tmp_path):
    import add_fact
    data = tmp_path / "data"
    data.mkdir()
    (data / "privacy_rules.json").write_text(json.dumps({"keywords": ["zorbak"]}))
    path = str(data / "general_facts.json")
    db = str(tmp_path / "none.db")
    clean = nf(statement="Innocent.", subject="x", visibility="normal", applies_to="adults")
    assert add_fact.resolve_privacy(clean, path, db).visibility == "normal"
    tainted = nf(statement="Innocent.", subject="x", visibility="normal", applies_to="Zorbak relatives")
    res = add_fact.resolve_privacy(tainted, path, db)
    assert res.visibility == "private" and "zorbak" in res.explain().lower()


def test_normal_db_includes_applies_to_scans_it_and_indexes_it():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("UPDATE facts SET applies_to = 'example population' WHERE id = 1")
        c.execute("UPDATE facts SET applies_to = 'Zorbak relatives' WHERE id = 2")
        c.execute("UPDATE fact_revisions SET applies_to = 'example population' WHERE fact_id = 1 AND revision = 1")
        c.commit()
        c.close()
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules(keywords=("zorbak",)))
        con = sqlite3.connect(path)
        assert con.execute("SELECT applies_to FROM facts WHERE id = 1").fetchone()[0] == "example population"
        assert con.execute("SELECT COUNT(*) FROM facts WHERE id = 2").fetchone()[0] == 0   # keyword in applies_to: left out
        assert [r[0] for r in con.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'example population'")] == [1]
        assert con.execute("SELECT applies_to FROM fact_revisions WHERE fact_id = 1 AND revision = 1").fetchone()[0] == "example population"
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE facts SET applies_to = '  ' WHERE id = 1")
