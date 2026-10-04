"""Issue #6 (library part): pending facts, review.list_pending / approve / reject.
Temp dirs and temp git repos only; nothing here touches real knowledge-private data."""
import ast
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys

import pytest

from test_fact_revisions import world, entry, T1, T2, T3  # noqa: F401  (world is a fixture)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture
def rw(world, monkeypatch):
    """The world fixture plus the review module, imported after it so both share `revisions`."""
    sys.modules.pop("review", None)
    import review
    world.review = review
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    return world


def pend(key, statement=None, **kw):
    return entry(key, statement or f"Pending {key}.", status="pending", **kw)


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if check:
        assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def make_repo(w):
    d = w.env.data_dir
    git(d, "init", "-q")
    git(d, "config", "user.name", "Test")
    git(d, "config", "user.email", "t@example.com")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init", "--allow-empty")
    return d


def file_hashes(w):
    out = {}
    for n in os.listdir(w.env.data_dir):
        if n.endswith(".json"):
            with open(os.path.join(w.env.data_dir, n), "rb") as f:
                out[n] = hashlib.sha256(f.read()).hexdigest()
    return out


def seed3(w):
    w.seed(general=[pend("p1", date_added=T1), pend("p2", date_added=T2), entry("a1", date_added=T1, status="active")])


# ---------------------------------------------------------------- new facts start pending

def test_a_new_fact_added_through_add_fact_is_pending_in_the_entry_and_after_a_build(rw):
    w = rw
    w.seed()
    path = os.path.join(w.env.data_dir, "general_facts.json")
    r = w.af.append_fact(w.af.NewFact("Fresh.", "alpha", "low", volatility="static"), data_path=path, db_path="/nonexistent")
    assert r.ok and r.entry["status"] == "pending"
    r2 = w.af.append_fact(w.af.NewFact("Reviewed.", "alpha", "low", volatility="static", status="active"), data_path=path, db_path="/nonexistent")
    assert r2.ok and r2.entry["status"] == "active"
    status = dict(w.build().execute("SELECT statement, status FROM facts").fetchall())
    assert status == {"Fresh.": "pending", "Reviewed.": "active"}


def test_legacy_entries_without_a_status_key_stay_active(rw):
    e = entry("old")
    assert "status" not in e
    rw.seed(general=[e])
    assert rw.build().execute("SELECT status FROM facts").fetchone()[0] == "active"


# ---------------------------------------------------------------- list_pending

def test_list_pending_is_oldest_first_and_carries_provenance(rw):
    w = rw
    w.seed(general=[pend("late", date_added=T3),
                    pend("early", date_added=T1, captured_via="mcp", session_id="s1", captured_at=T1, source_quote="said so"),
                    entry("act", date_added=T1)])
    con = w.build()
    rows = w.review.list_pending(con)
    assert [r["source_key"] for r in rows] == ["early", "late"]
    assert set(rows[0]) == {"id", "source_key", "subject", "statement", "visibility", "trust_level", "captured_via",
                            "session_id", "captured_at", "source_quote", "date_added"}
    assert rows[0]["captured_via"] == "mcp" and rows[0]["source_quote"] == "said so" and rows[0]["subject"] == "alpha"
    assert w.review.list_pending(con) == rows  # read-only and stable


def test_list_pending_is_empty_after_approval(rw):
    w = rw
    w.seed(general=[pend("p1")])
    assert w.review.approve("p1", data_dir=w.env.data_dir, commit=False).ok
    assert w.review.list_pending(w.build()) == []


# ---------------------------------------------------------------- approve

def test_approve_appends_one_revision_per_fact_and_never_edits_the_entries(rw):
    w = rw
    seed3(w)
    before = file_hashes(w)
    res = w.review.approve(["p1", "p2"], reason="looks right", via="cli", session_id="sess", data_dir=w.env.data_dir, commit=False)
    assert res.ok and [i.outcome for i in res.items] == ["approved", "approved"]
    assert file_hashes(w) == before  # byte-identical original files
    lines = w.log_lines()
    assert len(lines) == 2
    recs = [json.loads(l) for l in lines]
    assert [(r["source_key"], r["revision"], r["status"], r["change_reason"], r["changed_via"], r["session_id"]) for r in recs] == \
        [("p1", 2, "active", "looks right", "cli", "sess"), ("p2", 2, "active", "looks right", "cli", "sess")]
    con = w.build()
    hist = w.rv.get_history(con, "p1")
    assert [(h["revision"], h["status"]) for h in hist] == [(1, "pending"), (2, "active")]
    assert [r[0] for r in con.execute("SELECT status FROM facts ORDER BY source_key")] == ["active"] * 3
    assert w.review.list_pending(con) == []


def test_approve_by_fact_id_digit_string_and_all(rw):
    w = rw
    w.seed(general=[pend("p1"), pend("p2"), pend("p3"), pend("p4")])
    con = w.build()
    ids = {r[0]: r[1] for r in con.execute("SELECT source_key, id FROM facts")}
    res = w.review.approve([ids["p1"]], data_dir=w.env.data_dir, commit=False, db=con)
    assert [(i.source_key, i.outcome) for i in res.items] == [("p1", "approved")]
    res = w.review.approve([str(ids["p2"])], data_dir=w.env.data_dir, commit=False, db=con)
    assert [(i.source_key, i.outcome) for i in res.items] == [("p2", "approved")]
    res = w.review.approve("all", data_dir=w.env.data_dir, commit=False)
    assert [(i.source_key, i.outcome) for i in res.items] == [("p3", "approved"), ("p4", "approved")]


def test_approve_all_only_touches_pending_and_zero_pending_is_not_an_error(rw):
    w = rw
    w.seed(general=[entry("a1"), pend("p1")])
    res = w.review.approve("all", data_dir=w.env.data_dir, commit=False)
    assert res.ok and [(i.source_key, i.outcome) for i in res.items] == [("p1", "approved")]
    again = w.review.approve("all", data_dir=w.env.data_dir, commit=False)
    assert again.ok and again.items == [] and again.commit is None and "no pending facts" in again.notes
    assert len(w.log_lines()) == 1


def test_approving_twice_changes_nothing(rw):
    w = rw
    seed3(w)
    w.review.approve("p1", data_dir=w.env.data_dir, commit=False)
    with open(w.log) as f:
        log_before = f.read()
    res = w.review.approve("p1", data_dir=w.env.data_dir, commit=False)
    assert res.ok and res.items[0].outcome == "skipped" and "active" in res.items[0].reason
    with open(w.log) as f:
        assert f.read() == log_before


def test_unknown_blank_and_unresolvable_refs_are_reported_not_fatal(rw):
    w = rw
    seed3(w)
    res = w.review.approve(["nope", "", "   ", 999, "p1", None], data_dir=w.env.data_dir, commit=False)
    out = {i.ref: i.outcome for i in res.items}
    assert out["nope"] == "unknown" and out[""] == "unknown" and out["   "] == "unknown" and out["p1"] == "approved"
    assert out["999"] == "error"  # an id with no database to resolve it
    assert out["None"] == "unknown"
    assert not res.ok  # something was wrong ...
    assert len(w.log_lines()) == 1  # ... but the valid item went through


def test_unknown_id_with_a_database(rw):
    w = rw
    seed3(w)
    res = w.review.approve([12345], data_dir=w.env.data_dir, commit=False, db=w.build())
    assert res.items[0].outcome == "unknown" and "12345" in res.items[0].reason


def test_retracted_superseded_and_active_facts_are_skipped_with_a_reason(rw):
    w = rw
    w.seed(general=[pend("r"), pend("s"), pend("x"), entry("a")])
    assert w.append("r", {"status": "retracted"}).ok
    assert w.append("s", {"status": "superseded", "superseded_by": "x"}).ok
    n = len(w.log_lines())
    res = w.review.approve(["r", "s", "a"], data_dir=w.env.data_dir, commit=False)
    assert [i.outcome for i in res.items] == ["skipped"] * 3
    assert "retracted" in res.items[0].reason and "superseded" in res.items[1].reason and "active" in res.items[2].reason
    assert len(w.log_lines()) == n


def test_duplicate_refs_in_one_batch_approve_once(rw):
    w = rw
    seed3(w)
    res = w.review.approve(["p1", "p1"], data_dir=w.env.data_dir, commit=False)
    assert [i.outcome for i in res.items] == ["approved", "skipped"] and len(w.log_lines()) == 1


def test_a_corrupt_log_is_a_batch_error_and_writes_nothing(rw):
    w = rw
    seed3(w)
    with open(w.log, "w") as f:
        f.write("not json\n")
    res = w.review.approve("p1", data_dir=w.env.data_dir, commit=False)
    assert not res.ok and res.errors and res.items == []
    with open(w.log) as f:
        assert f.read() == "not json\n"


def test_approve_validates_reason_and_via(rw):
    w = rw
    seed3(w)
    res = w.review.approve("p1", reason="  ", data_dir=w.env.data_dir, commit=False)
    assert not res.ok and "reason" in res.errors[0] and not os.path.exists(w.log)
    res = w.review.approve("p1", via="Bad Via", data_dir=w.env.data_dir, commit=False)
    assert res.items[0].outcome == "error" and "via" in res.items[0].reason and not os.path.exists(w.log)


# ---------------------------------------------------------------- batch failure and races

def test_a_failing_item_does_not_roll_back_earlier_items_and_the_rest_still_run(rw, monkeypatch):
    w = rw
    w.seed(general=[pend("p1"), pend("p2"), pend("p3")])
    real = w.rv.append_revision

    def flaky(key, *a, **kw):
        if key == "p2":
            return w.rv.RevisionResult(False, ["disk on fire"])
        return real(key, *a, **kw)
    monkeypatch.setattr(w.review.revisions, "append_revision", flaky)
    d = make_repo(w)
    res = w.review.approve(["p1", "p2", "p3"], data_dir=d)
    assert [(i.source_key, i.outcome) for i in res.items] == [("p1", "approved"), ("p2", "error"), ("p3", "approved")]
    assert not res.ok and "disk on fire" in res.items[1].reason
    assert [json.loads(l)["source_key"] for l in w.log_lines()] == ["p1", "p3"]
    assert res.commit and git(d, "show", "--name-only", "--format=", "HEAD") == "fact_revisions.jsonl"
    assert git(d, "status", "--porcelain") == ""


def test_fact_retracted_by_another_process_after_the_check_is_not_reactivated(rw, monkeypatch):
    w = rw
    w.seed(general=[pend("p1")])
    real = w.rv.append_revision

    def racing(key, *a, **kw):
        assert real("p1", {"status": "retracted"}, "x", "cli", data_dir=w.env.data_dir).ok  # someone else gets in first
        return real(key, *a, **kw)
    monkeypatch.setattr(w.review.revisions, "append_revision", racing)
    res = w.review.approve("p1", data_dir=w.env.data_dir, commit=False)
    assert res.items[0].outcome == "skipped" and "no longer pending" in res.items[0].reason
    assert [json.loads(l)["status"] for l in w.log_lines()] == ["retracted"]


def test_fact_edited_by_another_process_keeps_its_edit_when_approved(rw, monkeypatch):
    w = rw
    w.seed(general=[pend("p1", "Original.")])
    real = w.rv.append_revision

    def racing(key, *a, **kw):
        assert real("p1", {"statement": "Reworded."}, "x", "cli", data_dir=w.env.data_dir).ok  # still pending, new text
        return real(key, *a, **kw)
    monkeypatch.setattr(w.review.revisions, "append_revision", racing)
    assert w.review.approve("p1", data_dir=w.env.data_dir, commit=False).items[0].outcome == "approved"
    last = json.loads(w.log_lines()[-1])
    assert (last["revision"], last["status"], last["statement"]) == (3, "active", "Reworded.")


def test_concurrent_approves_of_the_same_facts_write_each_revision_once(rw):
    w = rw
    w.seed(general=[pend(f"p{i}") for i in range(6)])
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda _: w.review.approve("all", data_dir=w.env.data_dir, commit=False), range(6)))
    approved = [i.source_key for r in results for i in r.items if i.outcome == "approved"]
    assert sorted(approved) == [f"p{i}" for i in range(6)]
    assert all(r.ok for r in results)
    assert len(w.log_lines()) == 6
    assert w.review.list_pending(w.build()) == []  # the log is still valid for the build


# ---------------------------------------------------------------- git

def test_one_commit_for_the_batch_containing_only_the_revision_log(rw):
    w = rw
    seed3(w)
    d = make_repo(w)
    head = git(d, "rev-parse", "HEAD")
    res = w.review.approve(["p1", "p2"], data_dir=d)
    assert res.ok and res.commit
    assert git(d, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert git(d, "show", "--name-only", "--format=", "HEAD") == "fact_revisions.jsonl"
    assert "approve" in git(d, "log", "-1", "--format=%s") and git(d, "status", "--porcelain") == ""


def test_dirty_tree_refuses_before_writing_unless_allow_dirty(rw):
    w = rw
    seed3(w)
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    res = w.review.approve("p1", data_dir=d)
    assert not res.ok and "uncommitted" in res.errors[0] and res.items == [] and not os.path.exists(w.log)
    res = w.review.approve("p1", data_dir=d, allow_dirty=True)
    assert res.ok and res.commit
    assert git(d, "show", "--name-only", "--format=", "HEAD") == "fact_revisions.jsonl"  # stray stays out
    assert git(d, "status", "--porcelain") == "?? stray.txt"


def test_nothing_to_approve_ignores_a_dirty_tree_and_makes_no_commit(rw):
    w = rw
    w.seed(general=[entry("a1")])
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    head = git(d, "rev-parse", "HEAD")
    res = w.review.approve("all", data_dir=d)
    assert res.ok and res.commit is None and not res.errors and git(d, "rev-parse", "HEAD") == head


def test_non_git_dir_writes_without_committing_and_says_so(rw):
    w = rw
    seed3(w)
    res = w.review.approve("p1", data_dir=w.env.data_dir)
    assert res.ok and res.commit is None and any("not inside a git repository" in n for n in res.notes)
    assert len(w.log_lines()) == 1


def test_commit_false_skips_git_entirely_even_on_a_dirty_tree(rw):
    w = rw
    seed3(w)
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    res = w.review.approve("p2", data_dir=d, commit=False)
    assert res.ok and res.commit is None and res.notes == [] and len(w.log_lines()) == 1


def test_commit_failure_is_reported_and_the_revisions_stay_written(rw):
    w = rw
    seed3(w)
    d = make_repo(w)
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho nope >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    res = w.review.approve("p1", data_dir=d)
    assert res.items[0].outcome == "approved" and res.commit is None
    assert res.commit_error and "NOT committed" in res.commit_error and not res.ok
    assert len(w.log_lines()) == 1


# ---------------------------------------------------------------- reject

def test_reject_appends_a_retracted_revision_and_requires_a_reason(rw):
    w = rw
    seed3(w)
    res = w.review.reject("p1", "", data_dir=w.env.data_dir, commit=False)
    assert not res.ok and res.errors and not os.path.exists(w.log)
    res = w.review.reject(["p1", "a1", "zzz"], "wrong", data_dir=w.env.data_dir, commit=False)
    assert [(i.ref, i.outcome) for i in res.items] == [("p1", "rejected"), ("a1", "skipped"), ("zzz", "unknown")]
    con = w.build()
    assert [(h["revision"], h["status"]) for h in w.rv.get_history(con, "p1")] == [(1, "pending"), (2, "retracted")]
    assert w.review.approve("p1", data_dir=w.env.data_dir, commit=False).items[0].outcome == "skipped"


# ---------------------------------------------------------------- results must not be discarded

def test_no_caller_discards_the_result_of_approve_or_reject():
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", ".claude", ".venv", "__pycache__", "node_modules"}]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    f = node.value.func
                    called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    owner = getattr(getattr(f, "value", None), "id", None)
                    if called in ("approve", "reject") and owner in ("review", "rw", "w"):
                        offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    assert offenders == [], f"result of review.approve/reject discarded at {offenders}"


def test_append_revision_expect_precondition(rw):
    w = rw
    w.seed(general=[pend("p1")])
    bad = w.rv.append_revision("p1", {"status": "active"}, "r", "cli", data_dir=w.env.data_dir, expect={"status": "active"})
    assert not bad.ok and bad.errors[0].startswith("precondition failed") and not os.path.exists(w.log)
    assert not w.rv.append_revision("p1", {"status": "active"}, "r", "cli", data_dir=w.env.data_dir, expect={"bogus": 1}).ok
    assert w.rv.append_revision("p1", {"status": "active"}, "r", "cli", data_dir=w.env.data_dir, expect={"status": "pending"}).ok
