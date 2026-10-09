"""#39: facts.kind, a controlled vocabulary on facts (observation ... unclassified).

The vocabulary is CHECK-enforced; the choice among kinds is self-reported guidance. Covers add_fact
(entry + CLI + validation), the schema CHECKs, ingest, revisions (reclassify = a revision, visible in
`show --as-of`), the query layers (`--kind` filter, row shape in knowledge and modes), the normal-only DB,
and the batch tool. Temp dirs and fixture dbs only.
"""
import json
import os
import sqlite3
import sys

import pytest

import add_fact
import revisions
from test_add_fact import REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)

VOCAB = ("observation", "measurement", "decision", "preference", "plan", "definition",
         "inference", "rule", "lesson", "unclassified")


@pytest.fixture(autouse=True)
def _restore_modules():
    before = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


def nf(**kw):
    base = dict(statement="S.", subject="x", trust_level="low", no_decay=True, recheck_rationale="r")
    base.update(kw)
    return add_fact.NewFact(**base)


def test_vocabulary_is_the_documented_one():
    assert add_fact.KIND_VALUES == VOCAB


def test_new_fact_defaults_to_unclassified_and_every_kind_is_accepted():
    assert nf().kind == "unclassified"
    for k in VOCAB:
        errors, _ = add_fact.validate_fact(nf(kind=k), db_path="/nonexistent")
        assert errors == [], k
        assert add_fact.build_entry(nf(kind=k))["kind"] == k


@pytest.mark.parametrize("bad", ["", "Decision", "guess", None, 5, " decision"])
def test_add_fact_rejects_an_invalid_kind(bad):
    errors, _ = add_fact.validate_fact(nf(kind=bad), db_path="/nonexistent")
    assert any("kind" in e for e in errors), errors


def test_cli_kind_flag_round_trips_and_rejects_unknown(tmp_path):
    from test_add_fact import Env
    env = Env(tmp_path)
    assert env.cli("Chose X.", "--subject", "x", "--trust", "low", "--kind", "decision").returncode == 0
    assert env.entries()[0]["kind"] == "decision"
    r = env.cli("Other.", "--subject", "x", "--trust", "low", "--kind", "guess")
    assert r.returncode == 2 and len(env.entries()) == 1


def test_schema_check_rejects_an_unknown_kind_on_facts_and_revisions():
    con = sqlite3.connect(":memory:")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (id, name, domain) VALUES (1, 's', 'g')")
    ins = ("INSERT INTO facts (subject_id, statement, trust_level, freshness, kind) "
           "VALUES (1, 'a', 'low', 'unreviewed', ?)")
    for k in VOCAB:
        con.execute(ins, (k,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("guess",))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, (None,))
    # a row that says nothing is unclassified
    con.execute("INSERT INTO facts (subject_id, statement, trust_level, freshness) VALUES (1, 'b', 'low', 'unreviewed')")
    assert con.execute("SELECT kind FROM facts WHERE statement = 'b'").fetchone()[0] == "unclassified"
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, "
                    "trust_level, status, visibility, kind) VALUES (1, 'k', 1, 't', 'v', 's', 'low', 'active', 'private', 'guess')")


def test_ingest_stores_kind_and_defaults_legacy_entries_to_unclassified(ingest):
    con = ingest.run04([F(kind="lesson"), F(statement="Legacy, no kind.")])
    assert [r[0] for r in con.execute("SELECT kind FROM facts ORDER BY id")] == ["lesson", "unclassified"]
    assert [r[0] for r in con.execute("SELECT kind FROM fact_revisions ORDER BY id")] == ["lesson", "unclassified"]
    con = ingest.run11([F(kind="rule")])
    assert con.execute("SELECT kind FROM facts").fetchone()[0] == "rule"


@pytest.mark.parametrize("bad", ["guess", None, "", 3])
def test_ingest_fails_loudly_on_an_invalid_kind(ingest, bad):
    for run in (ingest.run04, ingest.run11):
        with pytest.raises(Exception, match="kind") as e:  # by name: the fixture reloads modules
            run([F(kind=bad)])
        assert type(e.value).__name__ == "RevisionError"


def test_a_revision_can_reclassify_and_history_shows_it(ingest):
    items = [F(kind="observation", source_key="k-one", freshness="no-decay", recheck_rationale="no decay")]
    snap = revisions.entry_snapshot(dict(items[0], date_added="2026-01-01"))
    assert snap["kind"] == "observation"
    assert revisions.entry_snapshot({"statement": "x"})["kind"] == "unclassified"
    rec = {"source_key": "k-one", "revision": 2, "changed_at": "2026-02-01T00:00:00+00:00", "changed_via": "cli",
           "session_id": None, "change_reason": "reclassified", **snap, "kind": "decision"}
    rec = {k: rec[k] for k in revisions.REVISION_KEYS}
    assert revisions.validate_record_shape(rec) == []
    assert revisions.validate_record_shape(dict(rec, kind="guess"))
    bad = {k: v for k, v in rec.items() if k != "kind"}
    assert any("missing key" in e and "kind" in e for e in revisions.validate_record_shape(bad))


def _db(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (id, name, domain) VALUES (1, 's', 'g')")
    for i, (kind, vis) in enumerate(rows, start=1):
        con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, kind, visibility, source_key) "
                    "VALUES (?, 1, ?, 'low', 'unreviewed', ?, ?, ?)", (i, f"banana fact {i}", kind, vis, f"k{i}"))
    return con


def test_knowledge_filters_and_row_shape_carry_kind():
    import knowledge
    con = _db([("decision", "normal"), ("plan", "normal"), ("decision", "private")])
    assert [r["id"] for r in knowledge.list_facts(con, kind="decision")] == [1, 3]
    assert [r["id"] for r in knowledge.search_facts(con, "banana", kind="plan")] == [2]
    assert knowledge.list_facts(con, kind="rule") == []
    assert knowledge.list_facts(con)[0]["kind"] == "decision"
    assert knowledge.get_fact(con, 2)["kind"] == "plan"
    assert [r["id"] for r in knowledge.list_facts(con, kind="x' OR '1'='1")] == []


def test_modes_filter_and_row_shape_carry_kind_and_keep_the_gate():
    import modes
    con = _db([("decision", "normal"), ("plan", "normal"), ("decision", "private")])
    normal, private = modes.Session(mode=modes.Mode.normal), modes.Session(mode=modes.Mode.private)
    assert [r["id"] for r in modes.list_facts(normal, con, kind="decision")] == [1]   # fact 3 is private: still hidden
    assert [r["id"] for r in modes.list_facts(private, con, kind="decision")] == [1, 3]
    assert [r["id"] for r in modes.search_facts(normal, con, "banana", kind="plan")] == [2]
    assert modes.list_facts(normal, con)[0]["kind"] == "decision"
    assert modes.get_fact(normal, con, 1)["kind"] == "decision"


def test_cli_kind_filter_and_show(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    env.sql("UPDATE facts SET kind = 'decision' WHERE id = 1")
    rows = json.loads(env.cli("facts", "--kind", "decision", "--json").stdout)
    assert [r["id"] for r in rows] == [1] and rows[0]["kind"] == "decision"
    rows = json.loads(env.cli("search", "protein", "--kind", "decision", "--json").stdout)
    assert [r["id"] for r in rows] == [1]
    assert env.cli("facts", "--kind", "plan", "--json").stdout.strip() == "[]"
    assert "kind=decision" in env.cli("show", "1").stdout
    assert env.cli("facts", "--kind", "guess").returncode == 2


def test_show_as_of_prints_the_kind_from_the_revision(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    env.sql("UPDATE fact_revisions SET kind = 'observation' WHERE source_key = 'k-one' AND revision = 1")
    env.sql("UPDATE fact_revisions SET kind = 'decision' WHERE source_key = 'k-one' AND revision = 2")
    assert "Kind: observation" in env.cli("show", "1", "--as-of", "2026-03-01").stdout
    assert "Kind: decision" in env.cli("show", "1", "--as-of", "2026-07-01").stdout


def test_normal_db_includes_kind_on_facts_and_revisions():
    import leak_test
    import normal_db
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("UPDATE facts SET kind = 'decision' WHERE id = 1")
        c.execute("UPDATE fact_revisions SET kind = 'decision' WHERE fact_id = 1 AND revision = 1")
        c.commit()
        c.close()
        path, _ = normal_db.build_normal_atomic(full, d, privacy_rules())
        con = sqlite3.connect(path)
        assert con.execute("SELECT kind FROM facts WHERE id = 1").fetchone()[0] == "decision"
        assert con.execute("SELECT kind FROM facts WHERE id = 2").fetchone()[0] == "unclassified"
        assert {r[0] for r in con.execute("SELECT kind FROM fact_revisions")} <= set(VOCAB)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE facts SET kind = 'guess' WHERE id = 1")


def privacy_rules():
    import privacy
    return privacy.Rules()


def test_batch_items_take_kind(tmp_path):
    import facts_batch
    assert "kind" in facts_batch.ITEM_KEYS
