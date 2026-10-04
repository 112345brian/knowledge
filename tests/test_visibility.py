"""#20: per-fact visibility (private|normal): schema, ingest, add_fact.

The resolver that computes visibility (#31) is not built here; this only stores it.
"""
import ast
import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

from test_add_fact import Env, REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)

FACT_COLUMNS_BEFORE_VISIBILITY = [
    "id", "subject_id", "statement", "is_original_claim", "is_personal", "trust_level", "trust_rationale",
    "status", "superseded_by_fact_id", "provided_by", "date_added", "last_reviewed_at", "recheck_by",
    "recheck_rationale", "origin_file_id", "notes"]


def test_existing_fact_columns_are_all_still_present_and_in_order(ingest):
    cols = [r[1] for r in ingest.db().execute("PRAGMA table_info(facts)")]
    assert cols[:len(FACT_COLUMNS_BEFORE_VISIBILITY)] == FACT_COLUMNS_BEFORE_VISIBILITY


def _one_subject(con):
    con.execute("INSERT INTO subjects (name) VALUES ('s')")


def test_visibility_column_defaults_to_private_for_raw_inserts(ingest):
    con = ingest.db()
    _one_subject(con)
    con.execute("INSERT INTO facts (subject_id, statement, trust_level, freshness) VALUES (1, 'x', 'low', 'unreviewed')")
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "private"


@pytest.mark.parametrize("bad", ["public", "", "Private", "NORMAL"])
def test_check_constraint_rejects_invalid_visibility(ingest, bad):
    con = ingest.db()
    _one_subject(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO facts (subject_id, statement, trust_level, visibility, freshness) VALUES (1, 'x', 'low', ?, 'unreviewed')", (bad,))


def test_null_visibility_is_rejected(ingest):
    con = ingest.db()
    _one_subject(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO facts (subject_id, statement, trust_level, visibility, freshness) VALUES (1, 'x', 'low', NULL, 'unreviewed')")


def test_visibility_is_indexed(ingest):
    assert "idx_facts_visibility" in [r[1] for r in ingest.db().execute("PRAGMA index_list(facts)")]


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_defaults_to_private_and_honors_normal(ingest, run):
    con = getattr(ingest, run)([F(), F(statement="Open.", visibility="normal"),
                                F(statement="Explicit.", visibility="private"), F(statement="Null.", visibility=None)])
    got = {r["statement"]: r["visibility"] for r in con.execute("SELECT statement, visibility FROM facts")}
    assert got == {"A statement.": "private", "Open.": "normal", "Explicit.": "private", "Null.": "private"}


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_unhashable_visibility_is_skipped_not_a_crash(ingest, run):
    con = getattr(ingest, run)([F(visibility=["normal"]), F(visibility=1), F(statement="Fine.")])
    assert [r[0] for r in con.execute("SELECT statement FROM facts")] == ["Fine."]


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_skips_an_invalid_visibility_with_a_warning_not_a_crash_or_a_guess(ingest, run, capsys):
    con = getattr(ingest, run)([F(visibility="public"), F(statement="Fine.")])
    assert [r[0] for r in con.execute("SELECT statement FROM facts")] == ["Fine."]
    out = capsys.readouterr().out
    assert "visibility" in out and "public" in out


def test_valid_values_agree_across_schema_ingest_and_add_fact(ingest):
    schema = open(os.path.join(REPO, "schema.sql")).read()
    m = re.search(r"visibility\s+TEXT NOT NULL[^,]*?CHECK \(visibility IN \(([^)]*)\)\)", schema)
    in_schema = {v.strip().strip("'") for v in m.group(1).split(",")}
    import add_fact
    assert in_schema == {"private", "normal"}
    assert in_schema == set(ingest.mod04.VALID_VISIBILITY) == set(ingest.mod11.VALID_VISIBILITY) == set(add_fact.VALID_VISIBILITY)


# ---------------------------------------------------------------- add_fact

@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_cli_default_is_private_and_recorded_explicitly(env):
    assert env.cli("S.", "--subject", "x", "--no-decay", "--recheck-rationale", "no decay", "--trust", "low").returncode == 0
    assert env.entries()[0]["visibility"] == "private"


def test_cli_visibility_normal(env):
    assert env.cli("S.", "--subject", "x", "--no-decay", "--recheck-rationale", "no decay", "--trust", "low", "--visibility", "normal").returncode == 0
    assert env.entries()[0]["visibility"] == "normal"


@pytest.mark.parametrize("bad", ["public", "Normal", "", " normal"])
def test_cli_rejects_invalid_visibility_and_writes_nothing(env, bad):
    r = env.cli("S.", "--subject", "x", "--no-decay", "--recheck-rationale", "no decay", "--trust", "low", "--visibility", bad)
    assert r.returncode == 1 and "visibility" in r.stderr
    assert not os.path.exists(env.facts)


def test_function_api_rejects_invalid_visibility_including_non_strings(ingest):
    import add_fact
    for bad in ("public", None, 1, ["normal"]):
        errors, _ = add_fact.validate_fact(add_fact.NewFact(statement="s", subject="x", no_decay=True, recheck_rationale="no decay", trust_level="low", visibility=bad))
        assert any("visibility" in e for e in errors), bad


def test_new_fact_defaults_to_private_when_visibility_is_omitted(ingest):
    import add_fact
    assert add_fact.NewFact(statement="s", subject="x", no_decay=True, recheck_rationale="no decay", trust_level="low").visibility == "private"


def test_add_then_rebuild_round_trip(ingest):
    import add_fact
    path = os.path.join(ingest.env.data_dir, "general_facts.json")
    nodb = os.path.join(ingest.env.root, "none.db")
    add_fact.append_fact(add_fact.NewFact(statement="Open.", subject="x", no_decay=True, recheck_rationale="no decay", trust_level="low", visibility="normal"), data_path=path, db_path=nodb)
    add_fact.append_fact(add_fact.NewFact(statement="Closed.", subject="x", no_decay=True, recheck_rationale="no decay", trust_level="low"), data_path=path, db_path=nodb)
    ingest.mod11.DATA_DIR = ingest.env.data_dir
    con = ingest.db()
    ingest.mod11.run(con)
    got = {r["statement"]: r["visibility"] for r in con.execute("SELECT statement, visibility FROM facts")}
    assert got == {"Open.": "normal", "Closed.": "private"}


def test_callers_of_the_validating_functions_do_not_discard_the_result():
    """append_fact / validate_fact can refuse (e.g. a bad visibility). A caller that ignores the
    return value would silently drop that refusal, so every call outside add_fact.py's own
    definitions must use the result."""
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", ".claude", "tests", "__pycache__", "node_modules")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    f = node.value.func
                    fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if fname in ("append_fact", "validate_fact"):
                        offenders.append(f"{path}:{node.lineno}")
    assert offenders == []


def test_add_fact_main_checks_ok_before_reporting_success():
    src = open(os.path.join(REPO, "add_fact.py")).read()
    main_src = src[src.index("def main("):]
    assert "result.ok" in main_src
