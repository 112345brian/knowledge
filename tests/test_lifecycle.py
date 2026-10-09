"""Issue #8: lifecycle.supersede / retract and the `supersede` / `retract` commands.
Temp dirs and temp git repos only; nothing here touches real knowledge-private data or the live db."""
import ast
import concurrent.futures
import json
import os
import subprocess
import sys

import pytest

import claims_store
import clock
from test_fact_revisions import world, entry, T1, T2, T3  # noqa: F401  (world is a fixture)
from test_review import rw, make_repo, git, file_hashes, GIT_ENV  # noqa: F401  (rw is a fixture)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE = os.path.join(REPO, "knowledge.py")


@pytest.fixture
def lw(rw):
    """The review world plus lifecycle (imported after it so every module shares one `revisions`)."""
    sys.modules.pop("lifecycle", None)
    import lifecycle
    rw.lc = lifecycle
    return rw


def kw(w, **extra):
    return dict(data_dir=w.env.data_dir, commit=False, **extra)


def seed_two(w):
    w.seed(general=[entry("a", statement="Old fact A."), entry("b", statement="New fact B.")])


def history(con, w, key):
    return [(h["revision"], h["status"], h["superseded_by"], h["change_reason"]) for h in w.rs.get_history(con, key)]


# ---------------------------------------------------------------- supersede

def test_supersede_appends_one_revision_and_never_touches_the_entry(lw):
    w = lw
    seed_two(w)
    before = file_hashes(w)
    with clock.frozen(T3):
        res = w.lc.supersede("a", "b", "B replaces A", **kw(w))
    assert res.ok and res.outcome == "superseded" and res.source_key == "a"
    assert (res.revision["revision"], res.revision["status"], res.revision["superseded_by"]) == (2, "superseded", "b")
    assert res.revision["change_reason"] == "B replaces A" and res.revision["statement"] == "Old fact A."
    assert file_hashes(w) == before
    assert len(w.log_lines()) == 1


def test_after_a_rebuild_facts_shows_the_status_and_resolves_superseded_by_to_the_replacement_id(lw):
    w = lw
    seed_two(w)
    with clock.frozen(T3):
        assert w.lc.supersede("a", "b", "B replaces A", **kw(w)).ok
    con = w.build()
    ids = {r["source_key"]: r["id"] for r in con.execute("SELECT id, source_key FROM facts")}
    row = con.execute("SELECT status, superseded_by_fact_id FROM facts WHERE source_key = 'a'").fetchone()
    assert (row["status"], row["superseded_by_fact_id"]) == ("superseded", ids["b"])
    assert con.execute("SELECT status FROM facts WHERE source_key = 'b'").fetchone()[0] == "active"


def test_history_shows_the_revision_with_its_reason_and_as_of_before_it_still_shows_active(lw):
    w = lw
    seed_two(w)
    with clock.frozen(T3):
        assert w.lc.retract("a", "turned out wrong", **kw(w)).ok
    con = w.build()
    assert history(con, w, "a") == [(1, "active", None, "original entry"), (2, "retracted", None, "turned out wrong")]
    before = w.rs.get_fact_as_of(con, "a", "2026-10-02")   # between T1 (entry) and T3 (change)
    assert before["status"] == "active" and before["revision"] == 1
    assert w.rs.get_fact_as_of(con, "a", "2026-10-03")["status"] == "retracted"


def test_works_for_pilot_and_batch_facts_by_source_key_and_by_id_even_without_a_stored_key(lw):
    w = lw
    pilot = entry("pilot1", statement="Pilot fact.")
    legacy = {k: v for k, v in entry(statement="Legacy batch fact, no key yet.").items() if k != "source_key"}
    w.seed(general=[entry("g1", statement="General fact.")], pilot=[pilot])
    w.write("facts_batch3.json", [legacy])
    con = w.build()
    legacy_key = con.execute("SELECT source_key FROM facts WHERE statement LIKE 'Legacy%'").fetchone()[0]
    assert legacy_key.startswith("legacy-")
    legacy_id = con.execute("SELECT id FROM facts WHERE statement LIKE 'Legacy%'").fetchone()[0]

    r1 = w.lc.supersede("pilot1", "g1", "pilot superseded", **kw(w))
    r2 = w.lc.supersede(legacy_id, "g1", "batch superseded by id", db=con, **kw(w))
    assert r1.ok and r2.ok and r2.source_key == legacy_key
    con2 = w.build()
    rows = {r["source_key"]: (r["status"], r["superseded_by_fact_id"]) for r in con2.execute(
        "SELECT source_key, status, superseded_by_fact_id FROM facts")}
    g1_id = con2.execute("SELECT id FROM facts WHERE source_key='g1'").fetchone()[0]
    assert rows["pilot1"] == ("superseded", g1_id) and rows[legacy_key] == ("superseded", g1_id)


def test_supersede_refusals(lw):
    w = lw
    w.seed(general=[entry("a"), entry("b"), entry("c"), entry("p", status="pending"), entry("r", status="retracted")])
    k = kw(w)
    cases = [
        (("a", "a"), "cannot supersede itself"),
        (("a", "nope"), "replacement"),
        (("nope", "a"), "no fact with source_key"),
        (("a", "r"), "is retracted"),
        (("a", "p"), "pending"),
        (("p", "a"), "pending"),
        (("r", "a"), "retracted"),
        (("a", ""), "replacement: blank reference"),
    ]
    for (ref, by), msg in cases:
        res = w.lc.supersede(ref, by, "why", **k)
        assert not res.ok and res.outcome in ("refused", "unknown") and msg in res.reason, (ref, by, res)
    assert not os.path.exists(w.log)


def test_supersede_cycles_are_refused_direct_and_indirect(lw):
    w = lw
    w.seed(general=[entry("a"), entry("b"), entry("c")])
    k = kw(w)
    assert w.lc.supersede("a", "b", "x", **k).ok
    res = w.lc.supersede("b", "a", "back", **k)       # A->B->A
    assert res.outcome == "refused" and "cycle" in res.reason and not res.ok
    assert w.lc.supersede("b", "c", "next", **k).ok   # A->B->C
    res = w.lc.supersede("c", "a", "loop", **k)       # C->A->B->C
    assert res.outcome == "refused" and "cycle" in res.reason
    assert len(w.log_lines()) == 2
    w.build()  # the log is valid for the build


def test_supersede_same_replacement_twice_is_unchanged_and_a_new_replacement_repoints(lw):
    w = lw
    w.seed(general=[entry("a"), entry("b"), entry("c")])
    k = kw(w)
    assert w.lc.supersede("a", "b", "x", **k).ok
    again = w.lc.supersede("a", "b", "x", **k)
    assert again.ok and again.outcome == "unchanged" and not again.changed and len(w.log_lines()) == 1
    re = w.lc.supersede("a", "c", "actually C", **k)
    assert re.ok and re.revision["superseded_by"] == "c" and re.revision["revision"] == 3


def test_a_replacement_that_is_itself_superseded_is_allowed_with_a_note(lw):
    w = lw
    w.seed(general=[entry("a"), entry("b"), entry("c")])
    assert w.lc.supersede("b", "c", "x", **kw(w)).ok
    res = w.lc.supersede("a", "b", "y", **kw(w))
    assert res.ok and any("itself superseded" in n for n in res.notes)


# ---------------------------------------------------------------- retract

def test_retract_clears_superseded_by_and_a_second_retract_is_a_clean_no_change(lw):
    w = lw
    seed_two(w)
    assert w.lc.supersede("a", "b", "x", **kw(w)).ok
    res = w.lc.retract("a", "wrong after all", **kw(w))
    assert res.ok and (res.revision["status"], res.revision["superseded_by"]) == ("retracted", None)
    again = w.lc.retract("a", "again", **kw(w))
    assert again.ok and again.outcome == "unchanged" and len(w.log_lines()) == 2
    assert w.build().execute("SELECT status, superseded_by_fact_id FROM facts WHERE source_key='a'").fetchone()[:] == ("retracted", None)


def test_retract_a_pending_fact_is_allowed_like_reject_and_unknown_refs_are_reported(lw):
    w = lw
    w.seed(general=[entry("p", status="pending")])
    assert w.lc.retract("p", "no", **kw(w)).outcome == "retracted"
    for ref in ("zzz", "", "   ", "12345"):
        res = w.lc.retract(ref, "why", **kw(w))
        assert not res.ok and res.outcome in ("unknown", "error") and res.reason, ref
    assert len(w.log_lines()) == 1


def test_a_fact_id_needs_a_db(lw):
    w = lw
    seed_two(w)
    res = w.lc.retract(1, "why", **kw(w))
    assert not res.ok and res.outcome == "error" and "database" in res.reason
    con = w.build()
    fid = con.execute("SELECT id FROM facts WHERE source_key='a'").fetchone()[0]
    assert w.lc.retract(fid, "why", db=con, **kw(w)).ok
    assert w.lc.retract(9999, "why", db=con, **kw(w)).outcome == "unknown"


@pytest.mark.parametrize("reason", ["", "   ", None, 5])
def test_reason_is_required_for_both(lw, reason):
    w = lw
    seed_two(w)
    for res in (w.lc.retract("a", reason, **kw(w)), w.lc.supersede("a", "b", reason, **kw(w))):
        assert not res.ok and res.errors == ["reason is required"]
    assert not os.path.exists(w.log)


def test_reversal_is_another_revision_through_the_library(lw):
    w = lw
    seed_two(w)
    with clock.frozen(T2):
        assert w.lc.retract("a", "oops", **kw(w)).ok
    with clock.frozen(T3):
        assert w.rs.append_revision("a", {"status": "active", "superseded_by": None}, "restored", "cli",
                                    data_dir=w.env.data_dir).ok
    con = w.build()
    assert [h[1] for h in history(con, w, "a")] == ["active", "retracted", "active"]
    assert con.execute("SELECT status FROM facts WHERE source_key='a'").fetchone()[0] == "active"


# ---------------------------------------------------------------- claims audit

@pytest.mark.parametrize("how", ["retract", "supersede"])
def test_audit_claims_flags_a_claim_citing_the_fact_after_a_rebuild(lw, how):
    w = lw
    seed_two(w)
    if how == "retract":
        assert w.lc.retract("a", "wrong", **kw(w)).ok
    else:
        assert w.lc.supersede("a", "b", "replaced", **kw(w)).ok
    con = w.build()
    fid = con.execute("SELECT id FROM facts WHERE source_key='a'").fetchone()[0]
    con.execute("INSERT INTO claims (id, statement) VALUES (1, 'A claim resting on fact a')")
    con.execute("INSERT INTO claim_facts (claim_id, fact_id) VALUES (1, ?)", (fid,))
    rows = claims_store.audit_claims(con)
    assert [(r["claim_id"], r["fact_id"], r["reason"]) for r in rows] == [(1, fid, "retracted" if how == "retract" else "superseded")]


# ---------------------------------------------------------------- concurrency, clock, data problems

def test_concurrent_retracts_write_one_revision_and_every_call_is_ok(lw):
    w = lw
    seed_two(w)
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda _: w.lc.retract("a", "dup", **kw(w)), range(6)))
    assert all(r.ok for r in results)
    assert sorted(r.outcome for r in results) == ["retracted"] + ["unchanged"] * 5
    assert len(w.log_lines()) == 1
    w.build()


def test_a_fact_retracted_by_another_process_after_we_looked_is_not_superseded(lw, monkeypatch):
    w = lw
    seed_two(w)
    real = w.rs.append_revision
    state = {"raced": False}

    def racing(key, changes, *a, **k):
        if not state["raced"]:
            state["raced"] = True
            assert real("a", {"status": "retracted"}, "other process", "cli", data_dir=w.env.data_dir).ok
        return real(key, changes, *a, **k)

    monkeypatch.setattr(w.rs, "append_revision", racing)
    res = w.lc.supersede("a", "b", "mine", **kw(w))
    assert res.outcome == "refused" and "changed by another process" in res.reason and not res.ok
    assert [json.loads(l)["status"] for l in w.log_lines()] == ["retracted"]


def test_a_clock_going_backwards_is_refused_with_the_revision_message(lw):
    w = lw
    seed_two(w)
    with clock.frozen(T3):
        assert w.lc.supersede("a", "b", "x", **kw(w)).ok
    with clock.frozen(T2):
        res = w.lc.retract("a", "late", **kw(w))
    assert not res.ok and res.outcome == "error" and "clock went backwards" in res.reason
    assert len(w.log_lines()) == 1


def test_a_corrupt_log_or_entry_file_is_an_error_not_a_traceback(lw):
    w = lw
    seed_two(w)
    with open(w.log, "w") as f:
        f.write("{not json\n")
    res = w.lc.retract("a", "x", **kw(w))
    assert not res.ok and res.errors and "not valid JSON" in res.errors[0]


# ---------------------------------------------------------------- git

def test_one_commit_containing_only_the_revision_log(lw):
    w = lw
    seed_two(w)
    d = make_repo(w)
    head = git(d, "rev-parse", "HEAD")
    res = w.lc.supersede("a", "b", "x", data_dir=d)
    assert res.ok and res.commit
    assert git(d, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert git(d, "show", "--name-only", "--format=", "HEAD") == "fact_revisions.jsonl"
    assert git(d, "log", "-1", "--format=%s") == "supersede: a" and git(d, "status", "--porcelain") == ""
    res = w.lc.retract("b", "y", data_dir=d)
    assert git(d, "log", "-1", "--format=%s") == "retract: b"


def test_dirty_tree_is_refused_before_writing_unless_allowed(lw):
    w = lw
    seed_two(w)
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    res = w.lc.retract("a", "x", data_dir=d)
    assert not res.ok and res.errors and "uncommitted" in res.errors[0] and not os.path.exists(w.log)
    res = w.lc.retract("a", "x", data_dir=d, allow_dirty=True)
    assert res.ok and res.commit
    assert git(d, "show", "--name-only", "--format=", "HEAD") == "fact_revisions.jsonl"
    assert "stray.txt" in git(d, "status", "--porcelain")


def test_no_change_neither_checks_the_tree_nor_commits(lw):
    w = lw
    seed_two(w)
    d = make_repo(w)
    assert w.lc.retract("a", "x", data_dir=d).ok
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    head = git(d, "rev-parse", "HEAD")
    res = w.lc.retract("a", "x again", data_dir=d)
    assert res.ok and res.outcome == "unchanged" and res.commit is None and git(d, "rev-parse", "HEAD") == head


def test_failed_commit_leaves_the_revision_and_is_not_ok(lw):
    w = lw
    seed_two(w)
    d = make_repo(w)
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho nope >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    res = w.lc.retract("a", "x", data_dir=d)
    assert res.outcome == "retracted" and res.commit is None and not res.ok
    assert "NOT committed" in res.commit_error and len(w.log_lines()) == 1


def test_a_data_dir_outside_git_is_written_with_a_note(lw):
    w = lw
    seed_two(w)
    res = w.lc.retract("a", "x", data_dir=w.env.data_dir)
    assert res.ok and res.commit is None and any("not inside a git repository" in n for n in res.notes)


# ---------------------------------------------------------------- results must not be discarded

def _py_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", ".claude", ".venv", "__pycache__", "node_modules"}]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        yield path, ast.parse(f.read())
                except SyntaxError:
                    continue


def test_no_caller_discards_a_lifecycle_result_and_every_append_revision_result_is_checked():
    offenders = []
    for path, tree in _py_files():
        rel = os.path.relpath(path, REPO)
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                f = node.value.func
                called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                owner = getattr(getattr(f, "value", None), "id", None)
                if called in ("supersede", "retract", "set_visibility") and owner in ("lifecycle", "lc", "w"):
                    offenders.append(f"{rel}:{node.lineno} discards lifecycle.{called}")
                if called == "append_revision" and owner in ("revisions", "revisions_store", "rv", "rs", "w"):
                    offenders.append(f"{rel}:{node.lineno} discards append_revision")
        if rel in ("lifecycle.py", "review.py"):  # production callers of append_revision must read `.ok`
            for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                calls = [n for n in ast.walk(fn) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                         and getattr(n.value.func, "attr", None) == "append_revision"]
                for c in calls:
                    var = c.targets[0].id
                    if not any(isinstance(n, ast.Attribute) and n.attr == "ok" and getattr(n.value, "id", None) == var
                               for n in ast.walk(fn)):
                        offenders.append(f"{rel}:{c.lineno} never checks {var}.ok")
    with open(os.path.join(REPO, "cli_lifecycle.py")) as f:
        src = f.read()
    assert "res.ok" in src and "commit_error" in src  # the CLI's one reporting path reads both
    assert offenders == [], offenders


# ---------------------------------------------------------------- CLI

class Cli:
    def __init__(self, w):
        self.w = w

    def db_from_world(self):
        con = self.w.build()
        import sqlite3
        out = sqlite3.connect(self.w.env.db)
        con.backup(out)
        out.close()

    def run(self, *args):
        return subprocess.run([sys.executable, KNOWLEDGE, *args], env={**self.w.env.env, **GIT_ENV},
                              cwd=self.w.env.root, capture_output=True, text=True)


@pytest.fixture
def cli(lw):
    c = Cli(lw)
    seed_two(lw)
    c.db_from_world()
    return c


def test_cli_retract_and_supersede_by_id_and_key_with_json(cli):
    w = cli.w
    d = make_repo(w)
    r = cli.run("supersede", "1", "--by", "b", "--reason", "B is newer", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] and out["outcome"] == "superseded" and out["source_key"] == "a" and out["commit"]
    assert git(d, "status", "--porcelain") == ""
    r = cli.run("retract", "b", "--reason", "nope")
    assert r.returncode == 0 and r.stdout.startswith("retract: b -> revision 2") and "Rebuild" in r.stdout
    assert [json.loads(l)["status"] for l in w.log_lines()] == ["superseded", "retracted"]


def test_cli_exit_codes_and_missing_reason(cli):
    w = cli.w
    assert cli.run("retract", "a").returncode == 2                       # --reason is required
    assert cli.run("supersede", "a", "--reason", "x").returncode == 2    # --by is required
    r = cli.run("retract", "zzz", "--reason", "x")
    assert r.returncode == 1 and r.stderr.startswith("error: unknown:")
    r = cli.run("supersede", "a", "--by", "a", "--reason", "x")
    assert r.returncode == 1 and "cannot supersede itself" in r.stderr
    r = cli.run("retract", "a", "--reason", "   ")
    assert r.returncode == 1 and "reason is required" in r.stderr
    assert not os.path.exists(w.log)
    assert cli.run("retract", "a", "--reason", "x").returncode == 0
    r = cli.run("retract", "a", "--reason", "x")
    assert r.returncode == 0 and r.stdout.startswith("no change: a")


def test_cli_dirty_tree_and_commit_failure(cli):
    w = cli.w
    d = make_repo(w)
    with open(os.path.join(d, "stray.txt"), "w") as f:
        f.write("x")
    r = cli.run("retract", "a", "--reason", "x")
    assert r.returncode == 1 and "uncommitted" in r.stderr and not os.path.exists(w.log)
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    r = cli.run("retract", "a", "--reason", "x", "--allow-dirty")
    assert r.returncode == 3 and "NOT committed" in r.stderr and len(w.log_lines()) == 1


def test_cli_without_a_db_still_resolves_source_keys_but_not_ids(lw):
    w = lw
    seed_two(w)
    c = Cli(w)
    r = c.run("retract", "1", "--reason", "x")
    assert r.returncode == 1 and "database" in r.stderr
    assert c.run("retract", "a", "--reason", "x").returncode == 0
