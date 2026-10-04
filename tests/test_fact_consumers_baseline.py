"""Pins what every consumer of `facts` sees (views, FTS, claims seeding, knowledge.py reads).

Written BEFORE issue #30 changed the facts schema/ingest and green on the old code; it must stay
green after. Nothing here mentions source_key or revisions on purpose.
"""
import importlib.util
import os

from test_fact_ingest import F, ingest  # noqa: F401  (fixture)
from test_add_fact import REPO

ITEMS = [
    F(subject="alpha", statement="Whole-body BMD Z-score fell from -0.6 to -1.1.", trust_level="high",
      trust_rationale="dexa", notes="n1", recheck_by="2027-01-01", recheck_rationale="rr", visibility="normal"),
    F(subject="beta", statement="Zebra stripes are unique.", trust_level="low", is_original_claim=True),
]


def build(ingest):
    con = ingest.run04(ITEMS)
    con.execute("INSERT INTO claims (statement) VALUES ('c')")
    return con


def test_views_show_the_ingested_rows(ingest):
    con = build(ingest)
    assert [tuple(r) for r in con.execute("SELECT subject, statement, trust_level, status, date_added FROM v_facts ORDER BY id")] == [
        ("alpha", "Whole-body BMD Z-score fell from -0.6 to -1.1.", "high", "active", "2026-09-11"),
        ("beta", "Zebra stripes are unique.", "low", "active", "2026-09-11"),
    ]
    assert [tuple(r) for r in con.execute("SELECT fact_id, subject, status FROM v_fact_tags ORDER BY fact_id")] == [
        (1, "alpha", "active"), (2, "beta", "active")]
    assert [tuple(r) for r in con.execute("SELECT fact_id, subject, trust_level, recheck_by FROM fact_with_sources ORDER BY fact_id")] == [
        (1, "alpha", "high", "2027-01-01"), (2, "beta", "low", None)]


def test_fts_search_and_update_trigger(ingest):
    con = build(ingest)
    assert [r[0] for r in con.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'zebra'")] == [2]
    con.execute("UPDATE facts SET statement = 'Okapi stripes.' WHERE id = 2")
    assert con.execute("SELECT COUNT(*) FROM facts_fts WHERE facts_fts MATCH 'zebra'").fetchone()[0] == 0
    assert [r[0] for r in con.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'okapi'")] == [2]


def test_claims_seeding_matches_facts_by_statement_prefix(ingest):
    con = build(ingest)
    spec = importlib.util.spec_from_file_location("seed_claims", os.path.join(REPO, "05_seed_claims.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.CLAIMS = [dict(statement="X", notes="n", fact_match_prefixes=["Whole-body BMD Z-score fell from -0.6"])]
    mod.run(con)
    assert [tuple(r) for r in con.execute("SELECT fact_id FROM claim_facts WHERE claim_id = 2")] == [(1,)]


def test_knowledge_py_reads(ingest):
    con = build(ingest)
    spec = importlib.util.spec_from_file_location("knowledge_mod", os.path.join(REPO, "knowledge.py"))
    k = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(k)
    assert [r["id"] for r in k.search_facts(con, "zebra")] == [2]
    assert [r["id"] for r in k.list_facts(con)] == [1, 2]
    assert [r["id"] for r in k.list_facts(con, status="active", trust="high")] == [1]


def test_status_check_still_rejects_garbage(ingest):
    import sqlite3
    import pytest
    con = build(ingest)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE facts SET status = 'bogus' WHERE id = 1")
