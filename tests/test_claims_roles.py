"""#44: Toulmin structure on claims (warrant, qualifier) and a role on every claim_facts row
(grounds | backing | rebuttal), and what the premise audit does with each role.

The audit's new outcomes (a rebuttal row with a distinct reason and severity "info") are the kind a caller can
silently ignore, so a scan test fails when a module that calls audit_claims never looks at `severity`.
"""
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
from datetime import date

import pytest

import claims_audit as ca
import claims_store
from conftest import REPO

TODAY = date(2026, 10, 4)


def new_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (name) VALUES ('s')")
    return con


def add_fact(con, statement, status="active", recheck_by=None):
    return con.execute("INSERT INTO facts (subject_id, statement, trust_level, status, recheck_by, freshness) "
                       "VALUES (1, ?, 'medium', ?, ?, ?)", (statement, status, recheck_by, "recheck" if recheck_by else "unreviewed")).lastrowid


def add_claim(con, statement, links=(), **cols):
    keys = ["statement"] + list(cols)
    cid = con.execute(f"INSERT INTO claims ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", [statement] + list(cols.values())).lastrowid
    for link in links:
        fact, role, note = (link + (None, None))[:3] if isinstance(link, tuple) else (link, "grounds", None)
        con.execute("INSERT INTO claim_facts (claim_id, fact_id, role, note) VALUES (?, ?, ?, ?)", (cid, fact, role, note))
    return cid


# ------------------------------------------------------------------ schema

def test_defaults_and_checks():
    con = new_db()
    f1, f2 = add_fact(con, "a"), add_fact(con, "b")
    c = add_claim(con, "claim")
    con.execute("INSERT INTO claim_facts (claim_id, fact_id) VALUES (?, ?)", (c, f1))             # no role given
    assert con.execute("SELECT role, note FROM claim_facts").fetchone()[:] == ("grounds", None)
    assert tuple(con.execute("SELECT warrant, qualifier FROM claims").fetchone()) == (None, None)
    for role in ("backing", "rebuttal"):
        con.execute("DELETE FROM claim_facts")
        con.execute("INSERT INTO claim_facts (claim_id, fact_id, role) VALUES (?, ?, ?)", (c, f1, role))
    for bad in ("ground", "Grounds", "", None):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO claim_facts (claim_id, fact_id, role) VALUES (?, ?, ?)", (c, f2, bad))
    for blank in ("", "   ", "\n"):
        for sql in ("UPDATE claims SET warrant = ?", "UPDATE claims SET qualifier = ?", "UPDATE claim_facts SET note = ?"):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(sql, (blank,))
    con.execute("UPDATE claims SET warrant = 'because', qualifier = 'in adults'")


def test_a_fact_has_one_role_per_claim_but_may_differ_between_claims():
    con = new_db()
    f = add_fact(con, "shared")
    c1, c2 = add_claim(con, "one"), add_claim(con, "two")
    con.execute("INSERT INTO claim_facts (claim_id, fact_id, role) VALUES (?, ?, 'grounds')", (c1, f))
    con.execute("INSERT INTO claim_facts (claim_id, fact_id, role) VALUES (?, ?, 'rebuttal')", (c2, f))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO claim_facts (claim_id, fact_id, role) VALUES (?, ?, 'backing')", (c1, f))


def test_the_stale_premises_view_carries_the_role():
    con = new_db()
    f = add_fact(con, "old", status="retracted")
    add_claim(con, "c", [(f, "backing", None)])
    assert [tuple(r) for r in con.execute("SELECT claim_id, fact_id, reason, role FROM v_claims_with_stale_premises")] == [(1, f, "retracted", "backing")]


# ------------------------------------------------------------------ the audit by role

@pytest.mark.parametrize("role", ["grounds", "backing"])
@pytest.mark.parametrize("status", ["superseded", "retracted"])
def test_stale_grounds_and_backing_weaken_the_claim_with_the_old_reasons(role, status):
    con = new_db()
    f = add_fact(con, "x", status=status)
    add_claim(con, "c", [(f, role, "why linked")])
    (row,) = claims_store.audit_claims(con, TODAY)
    assert (row["role"], row["severity"], row["reason"], row["link_note"]) == (role, "weakens", status, "why linked")


@pytest.mark.parametrize("status, reason", [("superseded", "rebuttal_superseded"), ("retracted", "rebuttal_retracted")])
def test_a_stale_rebuttal_is_informational_with_a_distinct_reason(status, reason):
    con = new_db()
    f = add_fact(con, "counter-evidence", status=status)
    add_claim(con, "c", [(f, "rebuttal", None)])
    (row,) = claims_store.audit_claims(con, TODAY)
    assert (row["role"], row["severity"], row["reason"]) == ("rebuttal", "info", reason)


def test_a_rebuttal_past_its_recheck_by_is_informational_too():
    con = new_db()
    f = add_fact(con, "counter", recheck_by="2020-01-01")
    add_claim(con, "c", [(f, "rebuttal", None)])
    (row,) = claims_store.audit_claims(con, TODAY)
    assert (row["reason"], row["severity"], row["recheck_by"]) == ("rebuttal_past_recheck_by", "info", "2020-01-01")


def test_a_current_rebuttal_yields_no_row_and_default_rows_are_grounds_and_weaken():
    con = new_db()
    ok = add_fact(con, "fine")
    stale = add_fact(con, "stale", status="retracted")
    add_claim(con, "c", [(ok, "rebuttal", None)])
    c2 = add_claim(con, "d")
    con.execute("INSERT INTO claim_facts (claim_id, fact_id) VALUES (?, ?)", (c2, stale))     # role omitted, as before #44
    (row,) = claims_store.audit_claims(con, TODAY)
    assert (row["claim_id"], row["role"], row["severity"], row["reason"]) == (c2, "grounds", "weakens", "retracted")


def test_the_same_fact_is_grounds_for_one_claim_and_a_rebuttal_of_another():
    con = new_db()
    f = add_fact(con, "pivot", status="superseded")
    c1 = add_claim(con, "supported by it", [(f, "grounds", None)])
    c2 = add_claim(con, "attacked by it", [(f, "rebuttal", None)])
    rows = claims_store.audit_claims(con, TODAY)
    assert [(r["claim_id"], r["role"], r["severity"], r["reason"]) for r in rows] == \
        [(c1, "grounds", "weakens", "superseded"), (c2, "rebuttal", "info", "rebuttal_superseded")]


def test_all_roles_in_one_claim_are_reported_per_row():
    con = new_db()
    g, b, r = (add_fact(con, n, status="retracted") for n in "gbr")
    add_claim(con, "c", [(g, "grounds", None), (b, "backing", None), (r, "rebuttal", None)])
    assert [(x["role"], x["severity"]) for x in claims_store.audit_claims(con, TODAY)] == [("grounds", "weakens"), ("backing", "weakens"), ("rebuttal", "info")]


def test_vocabulary_constants_match_the_schema():
    con = new_db()
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name = 'claim_facts'").fetchone()[0]
    listed = re.search(r"CHECK \(role IN \(([^)]*)\)\)", sql).group(1)
    assert set(re.findall(r"'(\w+)'", listed)) == set(ca.ROLES)
    assert ca.REBUTTAL_REASONS == ("rebuttal_superseded", "rebuttal_retracted", "rebuttal_past_recheck_by")


# ------------------------------------------------------------------ callers must look at severity

def test_every_module_that_calls_audit_claims_looks_at_severity():
    """A new outcome that a caller can silently ignore needs a test that fails when one does. The CLI must not
    count informational rows as failures; any future caller (an MCP tool, the inbox) must do the same."""
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__", ".claude", "tests", "docs", ".tach")]
        for name in files:
            if name.endswith(".py") and name != "claims_audit.py":
                text = open(os.path.join(root, name), encoding="utf-8").read()
                if re.search(r"\baudit_claims\(", text) and "severity" not in text:
                    offenders.append(name)
    assert offenders == [], f"these call audit_claims without checking severity: {offenders}"


# ------------------------------------------------------------------ CLI

def cli_env(tmp_path):
    from test_cli_wiring import Env
    return Env(tmp_path)


def test_cli_default_roles_output_is_unchanged(tmp_path):
    env = cli_env(tmp_path)
    env.claim(1, "Protein timing matters", 4, inference="inductive")
    r = env.cli("audit-claims")
    assert r.returncode == 1
    assert r.stdout.splitlines()[0] == "claim #1 (inductive): Protein timing matters"
    assert r.stdout.splitlines()[1].startswith("  fact #4 superseded") and "[" not in r.stdout.splitlines()[1].split(":")[0]
    assert len(r.stdout.splitlines()) == 2


def test_cli_a_stale_rebuttal_alone_is_reported_but_exits_zero(tmp_path):
    env = cli_env(tmp_path)
    env.sql("INSERT INTO claims (id, statement) VALUES (1, 'The claim')")
    env.sql("INSERT INTO claim_facts (claim_id, fact_id, role, note) VALUES (1, 4, 'rebuttal', 'old counter-study')")
    r = env.cli("audit-claims")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fact #4 [rebuttal] rebuttal_superseded" in r.stdout and "informational" in r.stdout
    assert "linked because: old counter-study" in r.stdout
    j = json.loads(env.cli("audit-claims", "--json").stdout)
    assert j["stale_premises"][0]["role"] == "rebuttal" and j["stale_premises"][0]["severity"] == "info"
    assert j["stale_premises"][0]["link_note"] == "old counter-study"


def test_cli_a_stale_rebuttal_next_to_stale_grounds_still_fails(tmp_path):
    env = cli_env(tmp_path)
    env.sql("INSERT INTO claims (id, statement) VALUES (1, 'The claim')")
    env.sql("INSERT INTO claim_facts (claim_id, fact_id, role) VALUES (1, 4, 'rebuttal'), (1, 5, 'backing')")
    r = env.cli("audit-claims")
    assert r.returncode == 1 and "fact #5 [backing] retracted" in r.stdout and "fact #4 [rebuttal]" in r.stdout


def test_cli_json_carries_the_role_on_default_rows(tmp_path):
    env = cli_env(tmp_path)
    env.claim(1, "c", 4)
    row = json.loads(env.cli("audit-claims", "--json").stdout)["stale_premises"][0]
    assert (row["role"], row["severity"], row["reason"], row["link_note"]) == ("grounds", "weakens", "superseded", None)


# ------------------------------------------------------------------ 05_seed_claims

def load_seed():
    spec = importlib.util.spec_from_file_location("seed_claims", os.path.join(REPO, "05_seed_claims.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seeded(claims):
    mod = load_seed()
    mod.CLAIMS = claims
    con = new_db()
    for s in ("Alpha fact one", "Beta fact two", "Gamma fact three"):
        add_fact(con, s)
    mod.run(con)
    return con


def test_the_two_existing_claims_are_not_backfilled_beyond_defaults():
    mod = load_seed()
    con = new_db()
    for c in mod.CLAIMS:
        for prefix in c["fact_match_prefixes"]:
            assert isinstance(prefix, str)                                 # still plain prefixes: grounds
    for s in [p for c in mod.CLAIMS for p in c["fact_match_prefixes"]]:
        add_fact(con, s + " (fixture)")
    mod.run(con)
    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM claims WHERE warrant IS NOT NULL OR qualifier IS NOT NULL OR inference_type IS NOT NULL").fetchone()[0] == 0
    assert {r[0] for r in con.execute("SELECT role FROM claim_facts")} == {"grounds"} and con.execute("SELECT COUNT(*) FROM claim_facts").fetchone()[0] == 4
    assert con.execute("SELECT COUNT(*) FROM claim_facts WHERE note IS NOT NULL").fetchone()[0] == 0


def test_seed_accepts_warrant_qualifier_roles_and_notes():
    con = seeded([dict(statement="C", notes="n", warrant=" because physiology ", qualifier="in adults",
                       fact_match_prefixes=["Alpha", {"prefix": "Beta", "role": "backing", "note": "  explains the mechanism "},
                                            {"prefix": "Gamma", "role": "rebuttal"}])])
    assert tuple(con.execute("SELECT warrant, qualifier FROM claims").fetchone()) == ("because physiology", "in adults")
    assert [tuple(r) for r in con.execute("SELECT f.statement, cf.role, cf.note FROM claim_facts cf JOIN facts f ON f.id = cf.fact_id ORDER BY f.id")] == \
        [("Alpha fact one", "grounds", None), ("Beta fact two", "backing", "explains the mechanism"), ("Gamma fact three", "rebuttal", None)]


def test_seed_normalizes_blank_text_to_null():
    con = seeded([dict(statement="C", notes="n", warrant="  ", qualifier="", fact_match_prefixes=[{"prefix": "Alpha", "note": "   "}])])
    assert tuple(con.execute("SELECT warrant, qualifier FROM claims").fetchone()) == (None, None)
    assert con.execute("SELECT note FROM claim_facts").fetchone()[0] is None


@pytest.mark.parametrize("claim, msg", [
    (dict(statement="C", notes="n", fact_match_prefixes=[{"prefix": "Alpha", "role": "evidence"}]), "role 'evidence'"),
    (dict(statement="C", notes="n", fact_match_prefixes=[{"role": "grounds"}]), "'prefix'"),
    (dict(statement="C", notes="n", fact_match_prefixes=[5]), "string or a dict"),
    (dict(statement="C", notes="n", warrant=3, fact_match_prefixes=[]), "warrant must be text"),
])
def test_seed_rejects_bad_input_loudly(claim, msg):
    with pytest.raises(ValueError, match=msg):
        seeded([claim])


# ------------------------------------------------------------------ normal-only DB

def test_claims_stay_out_of_the_normal_db_entirely():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        names = {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
        assert "claims" not in names and "claim_facts" not in names
