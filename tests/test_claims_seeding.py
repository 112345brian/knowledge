"""Claim seeding split: normalization and the matching policy are pure (`claims_rules`, tested against a fake
repository), and the SQLite adapter (`claims_store`) does the INSERTs and keeps SQLite's LIKE behaviour exactly.
"""
import importlib.util
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import claims_rules
import claims_store
from conftest import REPO


def load_seed():
    spec = importlib.util.spec_from_file_location("seed_claims", os.path.join(REPO, "client", "seed_claims.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ normalization, against a fake port

class FakeCon:
    def __init__(self):
        self.commits = 0

    def cursor(self):
        return "cursor"

    def commit(self):
        self.commits += 1


class FakeStores:
    def __init__(self):
        self.calls = []

    def insert_claim(self, cur, claim):
        self.calls.append(("claim", claim))
        return len([c for c in self.calls if c[0] == "claim"])

    def link_claim_premise(self, cur, claim_id, premise):
        self.calls.append(("premise", claim_id, premise))
        return 0


def ctx_with(stores, inputs=None):
    return SimpleNamespace(stores=stores, inputs=inputs, locations=SimpleNamespace(private_data_dir="/data"))


def test_run_hands_the_repository_normalized_claims_and_premises():
    stores, con = FakeStores(), FakeCon()
    load_seed().run(con, ctx_with(stores), claims=[
        dict(statement="C", notes="n", warrant="  because ", qualifier="   ", inference_type="inductive",
             fact_match_prefixes=["Alpha", {"prefix": "Beta", "role": "backing", "note": "  why "},
                                  {"prefix": "Gamma", "role": "rebuttal", "note": " "}]),
        dict(statement="D", notes=None, fact_match_prefixes=[])])
    first = stores.calls[0][1]
    assert (first.statement, first.notes, first.inference_type, first.warrant, first.qualifier) == \
        ("C", "n", "inductive", "because", None)
    assert [(c[1], c[2].prefix, c[2].role, c[2].note) for c in stores.calls if c[0] == "premise"] == \
        [(1, "Alpha", "grounds", None), (1, "Beta", "backing", "why"), (1, "Gamma", "rebuttal", None)]
    second = [c[1] for c in stores.calls if c[0] == "claim"][1]
    assert (second.inference_type, second.warrant, second.qualifier, second.premises) == (None, None, None, ())
    assert con.commits == 1


def test_run_validates_every_claim_before_writing_any():
    stores, con = FakeStores(), FakeCon()
    good = dict(statement="ok", notes=None, fact_match_prefixes=["x"])
    bad = dict(statement="bad", notes=None, fact_match_prefixes=[{"prefix": "x", "role": "proof"}])
    with pytest.raises(ValueError, match="role 'proof' must be one of"):
        load_seed().run(con, ctx_with(stores), claims=[good, bad])
    assert stores.calls == [] and con.commits == 0


def test_run_reads_claims_json_through_the_inputs_port_when_none_given():
    class Inputs:
        def exists(self, path):
            return path == "/data/claims.json"

        def read_json(self, path):
            return {"version": 1, "claims": [dict(statement="C", notes=None, fact_match_prefixes=["p"])]}

    stores = FakeStores()
    load_seed().run(FakeCon(), ctx_with(stores, Inputs()))
    assert stores.calls[0][1].statement == "C"


def test_load_claims_missing_file_and_bad_payload():
    seed = load_seed()

    class Inputs:
        def __init__(self, exists, payload=None, error=None):
            self._e, self._p, self._err = exists, payload, error

        def exists(self, path):
            return self._e

        def read_json(self, path):
            if self._err:
                raise self._err
            return self._p
    assert seed.load_claims(Inputs(False), "/d") == []
    with pytest.raises(ValueError, match="expected version 1 with a claims list"):
        seed.load_claims(Inputs(True, {"version": 2, "claims": []}), "/d")
    with pytest.raises(ValueError, match="cannot read claims"):
        seed.load_claims(Inputs(True, error=json.JSONDecodeError("x", "", 0)), "/d")


@pytest.mark.parametrize("raw, message", [
    (dict(statement="s", notes=None, warrant=5, fact_match_prefixes=[]), "warrant must be text, not int"),
    (dict(statement="s", notes=None, qualifier=[], fact_match_prefixes=[]), "qualifier must be text, not list"),
    (dict(statement="s", notes=None, fact_match_prefixes=[{"prefix": ""}]), "must be a string or a dict with a 'prefix'"),
    (dict(statement="s", notes=None, fact_match_prefixes=[{"role": "grounds"}]), "must be a string or a dict with a 'prefix'"),
    (dict(statement="s", notes=None, fact_match_prefixes=[7]), "must be a string or a dict with a 'prefix'"),
    (dict(statement="s", notes=None, fact_match_prefixes=[{"prefix": "p", "note": 3}]), "note must be text, not int"),
])
def test_normalize_claim_rejects_bad_input(raw, message):
    with pytest.raises(ValueError, match=message):
        claims_rules.normalize_claim(raw)


def test_match_pattern_is_the_prefix_plus_a_wildcard_with_no_escaping():
    assert claims_rules.match_pattern("Example") == "Example%"
    assert claims_rules.match_pattern("50%_x") == "50%_x%"
    assert claims_rules.match_pattern("") == "%"


# ------------------------------------------------------------------ the SQLite adapter: LIKE is SQLite's, unchanged

def db_with_facts(*statements):
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (name) VALUES ('s')")
    for s in statements:
        con.execute("INSERT INTO facts (subject_id, statement, trust_level, freshness) VALUES (1, ?, 'medium', 'unreviewed')", (s,))
    return con


def linked(con, prefix, role="grounds", note=None):
    cur = con.cursor()
    cid = claims_store.insert_claim(cur, claims_rules.Claim("c", None, None, None, None, ()))
    n = claims_store.link_claim_premise(cur, cid, claims_rules.Premise(prefix, role, note))
    rows = [r[0] for r in con.execute(
        "SELECT f.statement FROM claim_facts cf JOIN facts f ON f.id = cf.fact_id WHERE cf.claim_id = ? ORDER BY f.id", (cid,))]
    assert n == len(rows)
    return rows


def test_like_is_case_insensitive_for_ascii_only():
    """SQLite's default LIKE: `a` matches `A`, but `é` does not match `É`. Python's str.lower/startswith would differ."""
    con = db_with_facts("Example premise", "EXAMPLE other", "Élan vital", "élan other")
    assert linked(con, "example") == ["Example premise", "EXAMPLE other"]
    assert linked(con, "Élan") == ["Élan vital"]


def test_percent_and_underscore_in_a_prefix_are_wildcards_not_literals():
    con = db_with_facts("100% sure", "1000 sure", "a_b", "axb", "ab")
    assert linked(con, "100%") == ["100% sure", "1000 sure"]  # % also matches the extra 0 in "1000 sure"
    assert linked(con, "a_b") == ["a_b", "axb"]


def test_prefix_anchors_at_the_start_and_an_empty_plain_prefix_matches_every_fact():
    con = db_with_facts("The premise", "Not the premise", "Other")
    assert linked(con, "The") == ["The premise"]
    assert linked(con, "") == ["The premise", "Not the premise", "Other"]
    assert linked(con, "Missing") == []


def test_link_stores_role_and_note_and_matches_facts_of_any_status():
    con = db_with_facts("Old premise", "New premise")
    con.execute("UPDATE facts SET status = 'superseded' WHERE id = 1")
    cur = con.cursor()
    cid = claims_store.insert_claim(cur, claims_rules.Claim("c", "n", "inductive", "w", "q", ()))
    assert claims_store.link_claim_premise(cur, cid, claims_rules.Premise("Old", "rebuttal", "why")) == 1
    assert con.execute("SELECT role, note FROM claim_facts").fetchall() == [("rebuttal", "why")]
    assert con.execute("SELECT statement, notes, inference_type, warrant, qualifier FROM claims").fetchall() == \
        [("c", "n", "inductive", "w", "q")]
