"""Issue #23: lifecycle.set_visibility and the `set-visibility` command.
Temp dirs and temp git repos only; nothing here touches real knowledge-private data or the live db."""
import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys

import pytest

import clock
import privacy
from test_fact_revisions import world, entry, T1, T2, T3  # noqa: F401  (world is a fixture)
from test_review import rw, make_repo, git, file_hashes  # noqa: F401  (rw is a fixture)
from test_lifecycle import lw, kw, history, Cli  # noqa: F401  (lw is a fixture)


def write_rules(w, tags=None, keywords=None):
    with open(os.path.join(w.env.data_dir, "privacy_rules.json"), "w") as f:
        json.dump({"version": 1, "subject_tags": tags or {}, "keywords": keywords or []}, f)


def vis(con, key):
    return con.execute("SELECT visibility FROM facts WHERE source_key = ?", (key,)).fetchone()[0]


# ---------------------------------------------------------------- raising and the basic revision

def test_raising_to_private_always_works_without_a_db_and_leaves_the_entry_alone(lw):
    w = lw
    w.seed(general=[entry("a", visibility="normal", statement="Plain statement.")])
    before = file_hashes(w)
    with clock.frozen(T3):
        res = w.lc.set_visibility("a", "private", "keep this to myself", **kw(w))
    assert res.ok and res.outcome == "visibility_set" and res.revision["visibility"] == "private"
    assert res.revision["change_reason"] == "keep this to myself" and res.revision["revision"] == 2
    assert file_hashes(w) == before
    con = w.build()
    assert vis(con, "a") == "private"


def test_history_shows_the_visibility_revision_and_as_of_before_it_shows_the_earlier_value(lw):
    w = lw
    w.seed(general=[entry("a", visibility="private", statement="Plain statement.")])
    con = w.build()
    with clock.frozen(T3):
        assert w.lc.set_visibility("a", "normal", "why", db=con, **kw(w)).ok
    con = w.build()
    hist = w.rv.get_history(con, "a")
    assert [(h["revision"], h["visibility"], h["change_reason"]) for h in hist] == [(1, "private", "original entry"), (2, "normal", "why")]
    assert w.rv.get_fact_as_of(con, "a", "2026-10-02")["visibility"] == "private"
    assert w.rv.get_fact_as_of(con, "a", "2026-10-03")["visibility"] == "normal"


def test_after_a_demotion_the_built_history_is_private_throughout_by_design(lw):
    """Step 12 raises every revision of a currently-private fact to private (history is never more
    visible than the fact), so as-of before a DEMOTION reads private in the db; the log line itself
    keeps the earlier 'normal'. Documented deviation from the issue's wording, see README."""
    w = lw
    w.seed(general=[entry("a", visibility="normal", statement="Plain statement.")])
    with clock.frozen(T3):
        assert w.lc.set_visibility("a", "private", "why", **kw(w)).ok
    con = w.build()
    assert [h["visibility"] for h in w.rv.get_history(con, "a")] == ["private", "private"]
    assert w.rv.get_fact_as_of(con, "a", "2026-10-02")["visibility"] == "private"
    assert [json.loads(l)["visibility"] for l in w.log_lines()] == ["private"]
    entry_vis = json.load(open(os.path.join(w.env.data_dir, "general_facts.json")))[0]["visibility"]
    assert entry_vis == "normal"  # the original entry still says what it said


def test_lowering_with_no_rule_in_the_way_works_and_a_rebuild_shows_normal(lw):
    w = lw
    w.seed(general=[entry("a", visibility="private", statement="Plain statement.")])
    con = w.build()
    res = w.lc.set_visibility("a", "normal", "ok to share", db=con, **kw(w))
    assert res.ok and res.outcome == "visibility_set" and res.revision["visibility"] == "normal"
    assert vis(w.build(), "a") == "normal"


def test_works_for_pilot_and_batch_facts_too(lw):
    w = lw
    legacy = {k: v for k, v in entry(statement="Legacy batch fact.", visibility="private").items() if k != "source_key"}
    w.seed(pilot=[entry("pilot1", statement="Pilot fact.")])
    w.write("facts_batch2.json", [legacy])
    con = w.build()
    legacy_id = con.execute("SELECT id FROM facts WHERE statement LIKE 'Legacy%'").fetchone()[0]
    assert w.lc.set_visibility("pilot1", "normal", "x", db=con, **kw(w)).ok
    assert w.lc.set_visibility(legacy_id, "normal", "y", db=con, **kw(w)).ok
    con2 = w.build()
    assert {r[0]: r[1] for r in con2.execute("SELECT statement, visibility FROM facts")} == {
        "Pilot fact.": "normal", "Legacy batch fact.": "normal"}


# ---------------------------------------------------------------- the privacy floor

def test_lowering_is_refused_for_a_tagged_subject_with_the_resolvers_explanation(lw):
    w = lw
    write_rules(w, tags={"alpha": "private"})
    w.seed(general=[entry("a", visibility="private")])
    con = w.build()
    res = w.lc.set_visibility("a", "normal", "try", db=con, **kw(w))
    assert not res.ok and res.outcome == "refused" and not res.changed
    assert "subject 'alpha' is tagged private" in res.reason and "would raise it again" in res.reason
    assert not os.path.exists(w.log)


def test_lowering_is_refused_when_an_ancestor_subject_is_tagged(lw):
    w = lw
    write_rules(w, tags={"parent-s": "private"})
    w.seed(general=[entry("a", visibility="private")])
    con = w.build()
    con.execute("INSERT INTO subjects (id, name, domain) VALUES (900, 'parent-s', 'test')")
    con.execute("UPDATE subjects SET parent_id = 900 WHERE name = 'alpha'")
    res = w.lc.set_visibility("a", "normal", "try", db=con, **kw(w))
    assert res.outcome == "refused" and "under 'parent-s', which is tagged private" in res.reason


def test_lowering_is_refused_for_a_keyword_hit_in_the_current_statement(lw):
    w = lw
    write_rules(w, keywords=["quenby"])
    w.seed(general=[entry("a", visibility="private", statement="Quenby likes tea.")])
    con = w.build()
    res = w.lc.set_visibility("a", "normal", "try", db=con, **kw(w))
    assert res.outcome == "refused" and "'quenby'" in res.reason
    # the check reads the statement as revised, not the original entry
    w.seed(general=[entry("b", visibility="normal", statement="Harmless.")])
    assert w.append("b", {"statement": "Now mentions Quenby.", "visibility": "private"}, "reworded", at=T2).ok
    con = w.build()
    res = w.lc.set_visibility("b", "normal", "try", db=con, **kw(w))
    assert res.outcome == "refused" and "'quenby'" in res.reason


def test_refusal_agrees_with_the_build_which_would_have_raised_it_again(lw):
    w = lw
    write_rules(w, keywords=["quenby"])
    w.seed(general=[entry("a", visibility="private", statement="Quenby again.")])
    assert w.append("a", {"visibility": "normal"}, "forced past the check", at=T2).ok
    assert vis(w.build(), "a") == "private"          # a silent no-op revision: why the command refuses


def test_lowering_without_a_db_is_refused_but_raising_is_fine(lw):
    w = lw
    w.seed(general=[entry("a", visibility="private"), entry("b", visibility="normal")])
    res = w.lc.set_visibility("a", "normal", "x", **kw(w))
    assert res.outcome == "refused" and "needs the built db" in res.reason and not res.ok
    assert w.lc.set_visibility("b", "private", "x", **kw(w)).ok


def test_a_broken_rules_file_refuses_lowering_instead_of_guessing(lw):
    w = lw
    with open(os.path.join(w.env.data_dir, "privacy_rules.json"), "w") as f:
        f.write("{oops")
    w.seed(general=[entry("a", visibility="private")])
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE subjects (id INTEGER, name TEXT, parent_id INTEGER);")
    res = w.lc.set_visibility("a", "normal", "x", db=con, **kw(w))
    assert res.outcome == "refused" and "privacy rules" in res.reason


def test_a_stale_db_without_the_subjects_table_refuses(lw):
    w = lw
    w.seed(general=[entry("a", visibility="private")])
    res = w.lc.set_visibility("a", "normal", "x", db=sqlite3.connect(":memory:"), **kw(w))
    assert res.outcome == "refused" and "subject tree" in res.reason


# ---------------------------------------------------------------- no change, bad input

def test_no_change_is_clean_for_both_directions(lw):
    w = lw
    w.seed(general=[entry("a", visibility="private"), entry("b", visibility="normal", statement="Plain.")])
    con = w.build()
    r1 = w.lc.set_visibility("a", "private", "same", db=con, **kw(w))
    r2 = w.lc.set_visibility("b", "normal", "same", db=con, **kw(w))
    for r in (r1, r2):
        assert r.ok and r.outcome == "unchanged" and r.revision is None and not r.changed
    assert not os.path.exists(w.log)


def test_no_change_does_not_check_the_tree_or_commit_even_when_dirty(lw):
    w = lw
    w.seed(general=[entry("a", visibility="private")])
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    head = git(d, "rev-parse", "HEAD")
    res = w.lc.set_visibility("a", "private", "same", data_dir=d)
    assert res.ok and res.outcome == "unchanged" and git(d, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("bad", ["public", "", "NORMAL", None, 1])
def test_a_visibility_other_than_normal_or_private_is_an_error(lw, bad):
    w = lw
    w.seed(general=[entry("a")])
    res = w.lc.set_visibility("a", bad, "x", **kw(w))
    assert not res.ok and res.errors and not os.path.exists(w.log)


@pytest.mark.parametrize("reason", ["", "  ", None])
def test_reason_is_required(lw, reason):
    w = lw
    w.seed(general=[entry("a", visibility="normal")])
    res = w.lc.set_visibility("a", "private", reason, **kw(w))
    assert not res.ok and res.errors == ["reason is required"] and not os.path.exists(w.log)


def test_unknown_blank_and_id_without_db_refs(lw):
    w = lw
    w.seed(general=[entry("a")])
    assert w.lc.set_visibility("zzz", "private", "x", **kw(w)).outcome == "unknown"
    assert w.lc.set_visibility("  ", "private", "x", **kw(w)).outcome == "unknown"
    res = w.lc.set_visibility(1, "private", "x", **kw(w))
    assert res.outcome == "error" and "database" in res.reason


def test_pending_and_retracted_facts_can_change_visibility(lw):
    w = lw
    w.seed(general=[entry("p", status="pending", visibility="normal"), entry("r", status="retracted", visibility="normal")])
    assert w.lc.set_visibility("p", "private", "x", **kw(w)).ok
    assert w.lc.set_visibility("r", "private", "x", **kw(w)).ok
    con = w.build()
    assert [(r[0], r[1], r[2]) for r in con.execute("SELECT source_key, status, visibility FROM facts ORDER BY source_key")] == [
        ("p", "pending", "private"), ("r", "retracted", "private")]


def test_concurrent_identical_calls_write_one_revision(lw):
    w = lw
    w.seed(general=[entry("a", visibility="normal")])
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda _: w.lc.set_visibility("a", "private", "dup", **kw(w)), range(6)))
    assert all(r.ok for r in results)
    assert sorted(r.outcome for r in results) == ["unchanged"] * 5 + ["visibility_set"]
    assert len(w.log_lines()) == 1


def test_a_visibility_change_keeps_the_other_fields_of_the_snapshot(lw):
    w = lw
    w.seed(general=[entry("a", visibility="normal", trust_rationale="why", notes="n", recheck_by="2027-01-01")])
    assert w.append("a", {"trust_level": "high"}, "up", at=T2).ok
    res = w.lc.set_visibility("a", "private", "x", **kw(w))
    assert (res.revision["trust_level"], res.revision["trust_rationale"], res.revision["notes"], res.revision["recheck_by"]) == (
        "high", "why", "n", "2027-01-01")


# ---------------------------------------------------------------- git

def test_one_commit_with_only_the_revision_log_and_a_clear_message(lw):
    w = lw
    w.seed(general=[entry("a", visibility="normal")])
    d = make_repo(w)
    head = git(d, "rev-parse", "HEAD")
    res = w.lc.set_visibility("a", "private", "x", data_dir=d)
    assert res.ok and res.commit
    assert git(d, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert git(d, "show", "--name-only", "--format=", "HEAD") == "fact_revisions.jsonl"
    assert git(d, "log", "-1", "--format=%s") == "set-visibility: a private"


def test_dirty_tree_refused_and_failed_commit_is_not_ok(lw):
    w = lw
    w.seed(general=[entry("a", visibility="normal")])
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    res = w.lc.set_visibility("a", "private", "x", data_dir=d)
    assert not res.ok and "uncommitted" in res.errors[0] and not os.path.exists(w.log)
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    res = w.lc.set_visibility("a", "private", "x", data_dir=d, allow_dirty=True)
    assert res.outcome == "visibility_set" and not res.ok and "NOT committed" in res.commit_error


# ---------------------------------------------------------------- the normal-only DB

def _normal_statements(w, tmp_path, name):
    """Rebuild the full db from the data files, build the normal-only db from it, list its facts."""
    import normal_db
    con = w.build()
    full = str(tmp_path / f"{name}-full.db")
    out = sqlite3.connect(full)
    con.backup(out)
    out.close()
    outdir = tmp_path / f"{name}-out"
    outdir.mkdir()
    path, _counts = normal_db.build_normal_atomic(full, str(outdir), privacy.load_rules(privacy.rules_path(w.env.data_dir)))
    n = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in n.execute("SELECT statement FROM facts"))
    finally:
        n.close()


def test_promoting_puts_the_fact_in_the_normal_db_after_a_rebuild_and_demoting_removes_it(lw, tmp_path):
    w = lw
    w.seed(general=[entry("a", visibility="private", statement="Shareable moss fact."),
                    entry("b", visibility="private", statement="Secret lichen fact.")])
    assert _normal_statements(w, tmp_path, "before") == []
    con = w.build()
    assert w.lc.set_visibility("a", "normal", "ok to share", db=con, **kw(w)).ok
    assert _normal_statements(w, tmp_path, "promoted") == ["Shareable moss fact."]
    assert w.lc.set_visibility("a", "private", "changed my mind", **kw(w)).ok
    assert _normal_statements(w, tmp_path, "demoted") == []


# ---------------------------------------------------------------- CLI

@pytest.fixture
def cli(lw):
    c = Cli(lw)
    lw.seed(general=[entry("a", visibility="private", statement="Plain statement."), entry("b", visibility="normal", statement="Other fact.")])
    c.db_from_world()
    return c


def test_cli_promote_by_id_and_demote_by_key_with_json(cli):
    w = cli.w
    d = make_repo(w)
    r = cli.run("set-visibility", "1", "normal", "--reason", "fine to share", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] and out["outcome"] == "visibility_set" and out["source_key"] == "a" and out["commit"]
    assert git(d, "status", "--porcelain") == ""
    r = cli.run("set-visibility", "b", "private", "--reason", "no")
    assert r.returncode == 0 and r.stdout.startswith("set-visibility: b -> revision 2") and "Rebuild" in r.stdout
    assert [json.loads(l)["visibility"] for l in w.log_lines()] == ["normal", "private"]


def test_cli_no_change_is_exit_0_and_writes_nothing(cli):
    r = cli.run("set-visibility", "a", "private", "--reason", "same")
    assert r.returncode == 0 and r.stdout.startswith("no change: a") and not os.path.exists(cli.w.log)


def test_cli_refusal_prints_the_explanation_and_exits_1(cli):
    write_rules(cli.w, tags={"alpha": "private"})
    r = cli.run("set-visibility", "a", "normal", "--reason", "try")
    assert r.returncode == 1 and r.stderr.startswith("error: refused:") and "tagged private" in r.stderr
    assert not os.path.exists(cli.w.log)


def test_cli_argument_errors_and_exit_codes(cli):
    assert cli.run("set-visibility", "a", "public", "--reason", "x").returncode == 2
    assert cli.run("set-visibility", "a", "normal").returncode == 2           # --reason is required
    r = cli.run("set-visibility", "a", "normal", "--reason", " ")
    assert r.returncode == 1 and "reason is required" in r.stderr
    r = cli.run("set-visibility", "nope", "normal", "--reason", "x")
    assert r.returncode == 1 and r.stderr.startswith("error: unknown:")
    d = make_repo(cli.w)
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    r = cli.run("set-visibility", "b", "private", "--reason", "x")
    assert r.returncode == 3 and "NOT committed" in r.stderr
