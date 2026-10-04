"""#36: behavior of the functions moved out of inbox.py into lifecycle / review, pinned on edge inputs
(unknown ref, blank reason, dirty tree, non-git dir, concurrent calls). The page-level behavior is
covered by tests/test_inbox.py; the CLI by test_inbox.py / test_lifecycle.py.
Temp dirs and temp git repos only."""
import concurrent.futures
import os

import pytest

from test_fact_revisions import world, entry, T1, T2, T3  # noqa: F401  (world is a fixture)
from test_review import rw, make_repo, git, pend  # noqa: F401  (rw is a fixture)
from test_lifecycle import lw, kw  # noqa: F401  (lw is a fixture)


def seed(w):
    w.seed(general=[pend("p1", "Plain one.", date_added=T1, visibility="normal"),
                    entry("a1", statement="Active one.", date_added=T1, status="active", visibility="normal")])


# ---------------------------------------------------------------- review.resolve_ref (was review._resolve)

def test_resolve_ref_is_public_and_reports_problems(lw):
    w = lw
    seed(w)
    states = w.review.current_states(w.env.data_dir)
    assert w.review.resolve_ref("p1", states, None) == ("p1", None)
    assert w.review.resolve_ref("nope", states, None)[1] == "no fact with source_key 'nope'"
    assert w.review.resolve_ref("  ", states, None)[1] == "blank reference"
    assert w.review.resolve_ref(True, states, None)[1] == "ref must be a fact id or a source_key"
    assert "need a database" in w.review.resolve_ref("5", states, None)[1]
    assert w.review.resolve_ref(999, states, w.build())[1] == "no fact with id 999"
    assert not hasattr(w.review, "_resolve")


# ---------------------------------------------------------------- lifecycle.edit_fact

def test_edit_fact_unknown_ref_blank_reason_and_nothing_to_edit(lw):
    w = lw
    seed(w)
    assert not w.lc.edit_fact("nope", "r", statement="x", **kw(w)).ok
    res = w.lc.edit_fact("p1", "  ", statement="New.", **kw(w))
    assert not res.ok and res.errors == ["reason is required"]
    assert w.lc.edit_fact("p1", "r", **kw(w)).errors == ["nothing to edit: give at least one field"]
    assert "must not be blank" in w.lc.edit_fact("p1", "r", statement=" ", **kw(w)).errors[0]
    assert "YYYY-MM-DD" in w.lc.edit_fact("p1", "r", recheck_by="soon", **kw(w)).errors[0]
    assert not os.path.exists(w.log)


def test_edit_fact_dirty_tree_is_refused_unless_allowed_and_non_git_dir_notes_it(lw):
    w = lw
    seed(w)
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    res = w.lc.edit_fact("p1", "r", statement="New.", data_dir=d)
    assert not res.ok and "uncommitted" in res.errors[0] and not os.path.exists(w.log)
    res = w.lc.edit_fact("p1", "r", statement="New.", data_dir=d, allow_dirty=True)
    assert res.ok and res.commit


def test_edit_fact_in_a_non_git_dir_writes_and_notes_it(lw):
    w = lw
    seed(w)
    res = w.lc.edit_fact("p1", "r", statement="New.", data_dir=w.env.data_dir)
    assert res.ok and res.commit is None and any("not inside a git repository" in n for n in res.notes)
    assert len(w.log_lines()) == 1


def test_edit_fact_concurrent_identical_edits_write_once_each_other_skipped(lw):
    w = lw
    seed(w)

    def go(i):
        return w.lc.edit_fact("p1", "r", statement="Same edit.", **kw(w))
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(go, range(6)))
    assert sum(1 for r in results if r.ok and r.outcome == "changed") == 1
    assert all(r.outcome in ("changed", "skipped", "error") for r in results)
    assert len(w.log_lines()) == 1


# ---------------------------------------------------------------- lifecycle.set_visibility(expect_status, raise_only)

def test_expect_status_pending_leaves_a_reviewed_fact_alone_and_writes_nothing(lw):
    w = lw
    seed(w)
    res = w.lc.set_visibility("a1", "private", "r", expect_status="pending", **kw(w))
    assert res.ok and res.outcome == "unchanged" and "not 'pending'" in res.reason
    assert not os.path.exists(w.log)
    res = w.lc.set_visibility("p1", "private", "r", expect_status="pending", **kw(w))
    assert res.ok and res.outcome == "visibility_set"
    assert w.log_lines() and '"status": "pending"' in w.log_lines()[-1]
    again = w.lc.set_visibility("p1", "private", "r", expect_status="pending", **kw(w))
    assert again.ok and again.outcome == "unchanged" and len(w.log_lines()) == 1


def test_expect_status_unknown_ref_and_default_is_unchanged_behavior(lw):
    w = lw
    seed(w)
    assert w.lc.set_visibility("nope", "private", "r", expect_status="pending", **kw(w)).outcome == "unknown"
    # without expect_status an active fact can be changed, as `set-visibility` always could
    assert w.lc.set_visibility("a1", "private", "r", **kw(w)).outcome == "visibility_set"


def test_raise_only_refuses_normal_without_writing_even_when_not_floored(lw):
    w = lw
    seed(w)
    w.lc.set_visibility("p1", "private", "r", **kw(w))
    n = len(w.log_lines())
    db = w.build()
    res = w.lc.set_visibility("p1", "normal", "r", raise_only=True, db=db, **kw(w))
    assert res.outcome == "refused" and not res.ok and "only raises" in res.reason
    # without a db the floor cannot be read: still refused, never written
    res = w.lc.set_visibility("p1", "normal", "r", raise_only=True, **kw(w))
    assert res.outcome == "refused" and "only raises" in res.reason
    assert len(w.log_lines()) == n
    # and without raise_only, lowering still works (db present, no rule)
    assert w.lc.set_visibility("p1", "normal", "r", db=db, **kw(w)).outcome == "visibility_set"


def test_concurrent_make_private_calls_write_once(lw):
    w = lw
    seed(w)

    def go(i):
        return w.lc.set_visibility("p1", "private", "r", expect_status="pending", raise_only=True, **kw(w))
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(go, range(6)))
    assert all(r.ok for r in results)
    assert sorted(r.outcome for r in results) == ["unchanged"] * 5 + ["visibility_set"]
    assert len(w.log_lines()) == 1


# ---------------------------------------------------------------- review.pending_view

def test_pending_view_edge_inputs(lw, tmp_path):
    w = lw
    seed(w)
    vm = w.review.pending_view(str(tmp_path / "missing.db"), w.env.data_dir)
    assert "does not exist" in vm["error"] and vm["facts"] == []
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    assert w.review.pending_view(str(bad), w.env.data_dir)["error"]
    import sqlite3
    db = tmp_path / "ok.db"
    con = w.build()
    out = sqlite3.connect(str(db))
    con.backup(out)
    out.close()
    vm = w.review.pending_view(str(db), w.env.data_dir)
    assert vm["error"] is None and [f["ref"] for f in vm["facts"]] == ["p1"]
    assert vm["facts"][0]["basis"].startswith(("private", "normal"))
    assert w.review.visibility_basis("s", "text", "normal", w.review.privacy.Rules()) == "normal: no privacy rule applies"
