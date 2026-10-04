"""#1: claims.inference_type + stale-premise audit.

Part 1 pins what the claims schema and 05_seed_claims.py did BEFORE this change (these passed on the
old code). Part 2 covers the new behaviour. Superseded/retracted facts are written directly into a
fixture db built from the real schema.sql, since nothing in the pipeline can set that status yet (#8).
"""
import importlib.util
import os
import sqlite3

import pytest

from conftest import REPO


def new_db():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (name) VALUES ('s')")
    return con


def add_fact(con, statement, status="active", recheck_by=None, **kw):
    cur = con.execute(
        "INSERT INTO facts (subject_id, statement, trust_level, status, recheck_by, trust_rationale, notes, superseded_by_fact_id, freshness)"
        " VALUES (1, ?, 'medium', ?, ?, ?, ?, ?, ?)",
        (statement, status, recheck_by, kw.get("trust_rationale"), kw.get("notes"), kw.get("superseded_by"),
         "recheck" if recheck_by else "unreviewed"))  # #7
    return cur.lastrowid


def add_claim(con, statement, fact_ids=(), **cols):
    keys = ["statement"] + list(cols)
    cur = con.execute(f"INSERT INTO claims ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                      [statement] + list(cols.values()))
    for f in fact_ids:
        con.execute("INSERT INTO claim_facts (claim_id, fact_id) VALUES (?, ?)", (cur.lastrowid, f))
    return cur.lastrowid


def load_seed():
    spec = importlib.util.spec_from_file_location("seed_claims", os.path.join(REPO, "05_seed_claims.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------- Part 1: pinned pre-change behaviour ----------------

def test_claims_original_columns_keep_their_order():
    cols = [r[1] for r in new_db().execute("PRAGMA table_info(claims)")]
    assert cols[:4] == ["id", "statement", "date_added", "notes"]


def test_claim_facts_cascade_on_claim_and_fact_delete():
    con = new_db()
    f1, f2 = add_fact(con, "a"), add_fact(con, "b")
    c = add_claim(con, "c", [f1, f2])
    con.execute("DELETE FROM facts WHERE id = ?", (f1,))
    assert con.execute("SELECT fact_id FROM claim_facts WHERE claim_id=?", (c,)).fetchall() == [(f2,)]
    con.execute("DELETE FROM claims WHERE id = ?", (c,))
    assert con.execute("SELECT COUNT(*) FROM claim_facts").fetchone()[0] == 0


def test_claim_may_have_no_facts_and_fact_may_back_several_claims():
    con = new_db()
    f = add_fact(con, "shared")
    add_claim(con, "lonely")
    c1, c2 = add_claim(con, "one", [f]), add_claim(con, "two", [f])
    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 3
    assert con.execute("SELECT claim_id FROM claim_facts WHERE fact_id=? ORDER BY 1", (f,)).fetchall() == [(c1,), (c2,)]


def test_seed_claims_links_facts_by_statement_prefix_and_tolerates_misses():
    con = new_db()
    seed = load_seed()
    first = seed.CLAIMS[0]["fact_match_prefixes"][0]
    add_fact(con, first + " and more text")
    seed.run(con)
    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == len(seed.CLAIMS)
    assert con.execute("SELECT COUNT(*) FROM claim_facts").fetchone()[0] == 1


def test_schema_has_no_claim_views_before_audit_view():
    # pins that claims/claim_facts are referenced by no other view, so adding a column cannot break one
    con = new_db()
    for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type='view'"):
        if name != "v_claims_with_stale_premises":
            assert "claim_facts" not in sql and "claims" not in sql.replace("is_original_claim", "")
