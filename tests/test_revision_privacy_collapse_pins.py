"""Extra pins for the revisions and privacy collapse onto `build_repositories_port`: the edges the first pins
(test_revision_privacy_link_pins) leave open, plus two structural scans.

Written against the code before the lane ports were removed, kept as the contract the foundation repositories must
keep: the commit points of the revision step, what a failing second pass leaves behind, a corrupt rules or entities
file, history chunking, and that nobody discards what the privacy pass reports.
"""
import ast
import json
import os
import sqlite3

import pytest

import privacy
import privacy_store
import revisions
import revisions_store
from appctx import locs
from test_add_fact import REPO
from test_revision_privacy_link_pins import (T1, T2, T3, add_fact, load_step, new_db, private_dir,  # noqa: F401
                                             revision, write_log)

STEP = "apply_fact_revisions.py"


def write_rules(private_dir, **rules):
    (private_dir / "privacy_rules.json").write_text(json.dumps({"version": 1, "subject_tags": {}, "keywords": [], **rules}))


class CountingConnection:
    """Wraps a sqlite connection and counts `commit()` calls; everything else passes through."""
    def __init__(self, con):
        self._con = con
        self.commits = 0

    def commit(self):
        self.commits += 1
        self._con.commit()

    def __getattr__(self, name):
        return getattr(self._con, name)


# ------------------------------------------------------------------------------------------ commit points

def test_revision_step_commits_twice_the_log_then_the_floor_and_history(private_dir):
    con = new_db()
    add_fact(con, "a")
    con.commit()
    write_log(str(private_dir / "fact_revisions.jsonl"), revision("a", 2, T2, statement="Quenby now."))
    write_rules(private_dir, keywords=["quenby"])
    counted = CountingConnection(con)
    load_step(STEP).run(counted, locs(private_data_dir=str(private_dir)))
    assert counted.commits == 2
    assert con.in_transaction is False


def test_revision_step_with_nothing_to_do_still_commits_twice(private_dir):
    con = new_db()
    add_fact(con, "a")
    con.commit()
    counted = CountingConnection(con)
    load_step(STEP).run(counted, locs(private_data_dir=str(private_dir)))
    assert counted.commits == 2


def test_the_log_is_committed_before_a_corrupt_rules_file_fails_the_step(private_dir):
    """The first commit is the log's: a rules file that cannot be read fails the step AFTER the revisions are
    durable and BEFORE the floor touches anything."""
    con = new_db()
    add_fact(con, "a", statement="About Quenby.")
    con.commit()
    write_log(str(private_dir / "fact_revisions.jsonl"), revision("a", 2, T2, statement="About Quenby, revised."))
    (private_dir / "privacy_rules.json").write_text("{not json")
    counted = CountingConnection(con)
    with pytest.raises(privacy.PrivacyRulesError, match="not valid JSON"):
        load_step(STEP).run(counted, locs(private_data_dir=str(private_dir)))
    assert counted.commits == 1
    assert con.in_transaction is False
    assert con.execute("SELECT statement, visibility FROM facts").fetchone()[:] == ("About Quenby, revised.", "normal")
    assert con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 2


def test_a_corrupt_entities_file_beside_the_rules_fails_closed_before_the_floor(private_dir):
    con = new_db()
    add_fact(con, "a", statement="About Quenby.")
    con.commit()
    write_rules(private_dir, keywords=["quenby"])
    (private_dir / "entities.json").write_text("{not json")
    with pytest.raises(privacy.PrivacyRulesError):
        load_step(STEP).run(con, locs(private_data_dir=str(private_dir)))
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "normal"


# ------------------------------------------------------------------------------------------ second attempt

def test_a_second_pass_over_the_same_log_is_refused_and_changes_nothing(private_dir):
    """Today the refusal is the table's UNIQUE(source_key, revision), not a RevisionError: the validator reads only
    revision 1 as the original, so it accepts the log again and the first insert hits the constraint."""
    con = new_db()
    add_fact(con, "a")
    con.commit()
    write_log(str(private_dir / "fact_revisions.jsonl"), revision("a", 2, T2, statement="A2"))
    ctx = locs(private_data_dir=str(private_dir))
    load_step(STEP).run(con, ctx)
    before = con.execute("SELECT statement FROM facts").fetchone()[0], con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="fact_revisions.source_key, fact_revisions.revision"):
        load_step(STEP).run(con, ctx)
    after = con.execute("SELECT statement FROM facts").fetchone()[0], con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0]
    assert before == after == ("A2", 2)


def test_apply_revisions_failing_on_its_second_record_writes_none_of_them(tmp_path):
    con = new_db()
    add_fact(con, "a")
    con.commit()
    log = tmp_path / "log.jsonl"
    write_log(log, revision("a", 2, T2, statement="A2"), revision("a", 4, T3, statement="A4"))
    with pytest.raises(revisions.RevisionError, match="expected 3"):
        revisions_store.apply_revisions(con, str(log))
    assert con.execute("SELECT statement FROM facts").fetchone()[0] == "Plain."
    assert con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 1


# ------------------------------------------------------------------------------------------ history raising

def test_history_is_raised_for_every_private_fact_beyond_one_sql_chunk(private_dir):
    con = new_db()
    ids = [add_fact(con, f"k{i}", visibility="private" if i % 3 else "normal") for i in range(1300)]
    con.commit()
    load_step(STEP).run(con, locs(private_data_dir=str(private_dir)))
    rows = con.execute("SELECT f.visibility, r.visibility FROM fact_revisions r JOIN facts f ON f.id = r.fact_id").fetchall()
    assert len(rows) == len(ids)
    assert all(h == "private" for f, h in rows if f == "private")
    assert all(h == "normal" for f, h in rows if f == "normal")      # a normal fact's history is untouched


def test_history_of_a_private_fact_is_raised_but_an_already_private_snapshot_stays(private_dir):
    con = new_db()
    fid = add_fact(con, "a", visibility="private")
    revisions_store.insert_revision_row(con.cursor(), fid, revision("a", 2, T2, visibility="normal"))
    con.commit()
    load_step(STEP).run(con, locs(private_data_dir=str(private_dir)))
    assert [r[0] for r in con.execute("SELECT visibility FROM fact_revisions ORDER BY revision")] == ["private", "private"]


# ------------------------------------------------------------------------------------------ edge inputs

def test_a_blank_or_nul_rules_file_name_is_an_empty_or_unreadable_rules_set(tmp_path):
    assert privacy_store.load_rules(str(tmp_path / "absent.json")) == privacy.Rules()
    blank = tmp_path / "blank.json"
    blank.write_text("")
    with pytest.raises(privacy.PrivacyRulesError):
        privacy_store.load_rules(str(blank))
    obj = tmp_path / "partial.json"
    obj.write_text(json.dumps({"version": 1}))
    assert privacy_store.load_rules(str(obj)) == privacy.Rules()


def test_apply_rules_with_a_nul_in_a_keyword_or_statement_does_not_fail():
    con = new_db()
    add_fact(con, "a", statement="before\x00after Quenby")
    out = privacy_store.apply_rules_to_db(con, privacy.Rules(keywords=("quenby", "x\x00y")))
    assert [f for f, _ in out["raised"]] == [1]
    assert out["private_subjects"] == 0


def test_apply_rules_returns_raised_facts_in_id_order_with_their_explanation():
    con = new_db()
    for i in range(5):
        add_fact(con, f"k{i}", statement="Quenby." if i in (1, 3) else "Plain.")
    out = privacy_store.apply_rules_to_db(con, privacy.Rules(keywords=("quenby",)))
    assert [f for f, _ in out["raised"]] == [2, 4]
    assert all(isinstance(why, str) and why for _, why in out["raised"])


# ------------------------------------------------------------------------------------------ structural scans

def _python_sources():
    root = REPO
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "tests", ".claude", "__pycache__", "node_modules"}]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def test_no_caller_discards_what_the_privacy_pass_reports():
    """`apply_rules_to_db` / `apply_rules` / `apply_privacy_floor` return the facts they raised. A call used as a
    bare statement throws that away: the revision step prints it and the fact ingest counts it, so nothing else
    may drop it."""
    names = {"apply_rules_to_db", "apply_rules", "apply_privacy_floor", "apply_privacy_rules"}
    offenders = []
    for path in _python_sources():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = node.value.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if called in names:
                    offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    assert offenders == []


def test_only_the_revision_and_privacy_adapters_write_the_history_and_visibility_columns():
    """The SQL that writes `fact_revisions` or raises `facts.visibility` lives in two adapters; a decision about
    when to write it lives in the services and rules."""
    # leak_test.py (the normal-db lane) builds its own probe database and inserts history rows itself; out of scope here.
    homes = {"revisions_store.py", "privacy_store.py", "fact_ingest_store.py", "fact_tables_store.py", "leak_test.py"}
    needles = ("INSERT INTO fact_revisions", "UPDATE fact_revisions", "SET visibility = 'private'", "UPDATE subjects SET private")
    offenders = []
    for path in _python_sources():
        if os.path.basename(path) in homes:
            continue
        text = open(path, encoding="utf-8").read()
        offenders += [f"{os.path.relpath(path, REPO)}: {n}" for n in needles if n in text]
    assert offenders == []
