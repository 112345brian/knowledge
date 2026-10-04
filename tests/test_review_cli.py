"""Issue #6, CLI part: `review-pending`, `approve`, `reject` (thin printers over review.py).

The library semantics are covered in test_review.py; this file pins the commands: output, --json,
exit codes (batch error / unknown / error item -> 1, usage -> 2, commit failure -> 3, skipped -> 0),
dirty-tree refusal, commit failure, and the edge inputs. Temp dirs and temp git repos only
(GIT_CONFIG_GLOBAL=/dev/null, as in test_private_git.py); the real knowledge-private data and the
live db are never touched.
"""
import json
import os
import sqlite3
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_knowledge import Env as BaseEnv, ok  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
T1, T2 = "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00"
SCHEMA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


class Env(BaseEnv):
    """BaseEnv's db plus data files and facts: p-a (id 6) and p-b (id 7) pending, p-diary (id 8)
    pending in a subject tagged private, act-1 (id 9) active; ids 1-5 have no data-file entry."""

    def __init__(self, root, with_db=True, repo=True):
        super().__init__(root, with_db=with_db)
        self.env = {**self.env, **GIT_ENV}
        self.data_dir = os.path.join(self.root, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        entries = [
            self.entry("p-a", "Pending A about whey.", status="pending", date_added=T1),
            self.entry("p-b", "Pending B about whey.", status="pending", date_added=T2, captured_via="mcp", source_quote="said so"),
            self.entry("p-diary", "Secret diary line.", subject="diary", status="pending", date_added=T2),
            self.entry("act-1", "Already active.", status="active", date_added=T1),
        ]
        with open(os.path.join(self.data_dir, "general_facts.json"), "w") as f:
            json.dump(entries, f)
        if with_db:
            self.sql("INSERT INTO subjects (id, name, domain, private) VALUES (5, 'diary', 'personal', 1)")
            for i, e in enumerate(entries, start=6):
                self.sql("""INSERT INTO facts (id, subject_id, statement, is_personal, trust_level, status, source_key,
                                               date_added, captured_via, source_quote, volatility)
                            VALUES (?, ?, ?, 0, 'low', ?, ?, ?, ?, ?, 'static')""",
                         i, 5 if e["subject"] == "diary" else 2, e["statement"], e["status"], e["source_key"],
                         e["date_added"], e.get("captured_via"), e.get("source_quote"))
        if repo:
            git(self.data_dir, "init", "-q")
            git(self.data_dir, "config", "user.name", "Test")
            git(self.data_dir, "config", "user.email", "t@example.com")
            git(self.data_dir, "add", "-A")
            git(self.data_dir, "commit", "-q", "-m", "init")

    @staticmethod
    def entry(key, statement, subject="protein", **kw):
        e = {"source_key": key, "subject": subject, "statement": statement, "trust_level": "low",
             "is_original_claim": False, "is_personal": False, "visibility": "private",
             "volatility": "static"}
        e.update(kw)
        return e

    def sql(self, q, *params):
        con = sqlite3.connect(self.db)
        con.execute(q, params)
        con.commit()
        con.close()

    @property
    def log_path(self):
        return os.path.join(self.data_dir, "fact_revisions.jsonl")

    def log(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def head_files(self):
        return git(self.data_dir, "show", "--name-only", "--format=", "HEAD").splitlines()

    def n_commits(self):
        return int(git(self.data_dir, "rev-list", "--count", "HEAD"))

    def make_dirty(self):
        with open(os.path.join(self.data_dir, "stray.txt"), "w") as f:
            f.write("uncommitted\n")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---------------------------------------------------------------- review-pending

def test_review_pending_text_lists_oldest_first_with_provenance(env):
    out = ok(env.cli("review-pending"))
    lines = out.splitlines()
    assert lines[0].startswith("#6     [protein] (low)  p-a  added " + T1)
    assert lines[1] == "       Pending A about whey."
    assert lines[2].startswith("#7     [protein] (low)  p-b  added " + T2) and "via=mcp" in lines[2]
    assert "quote: said so" in out
    assert "Already active" not in out  # active facts are not in the queue
    assert out.rstrip().endswith("3 pending. Approve with `approve REF...` (or `--all`), reject with `reject REF... --reason TEXT`.")


def test_review_pending_json(env):
    rows = json.loads(ok(env.cli("review-pending", "--json")))
    assert [r["source_key"] for r in rows] == ["p-a", "p-b", "p-diary"]
    assert rows[1]["captured_via"] == "mcp" and rows[1]["source_quote"] == "said so" and rows[0]["id"] == 6


def test_review_pending_with_nothing_pending_and_with_an_empty_db(env, tmp_path):
    env.sql("UPDATE facts SET status = 'active'")
    assert ok(env.cli("review-pending")) == "No pending facts.\n"
    assert json.loads(ok(env.cli("review-pending", "--json"))) == []
    e = BaseEnv(tmp_path / "empty", with_db=False)
    con = sqlite3.connect(e.db)
    with open(SCHEMA) as f:
        con.executescript(f.read())
    con.close()
    assert ok(e.cli("review-pending")) == "No pending facts.\n"


def test_review_pending_missing_db(tmp_path):
    e = Env(tmp_path, with_db=False)
    r = e.cli("review-pending")
    assert r.returncode == 1 and r.stdout == "" and r.stderr.startswith("error: ") and "build" in r.stderr


def test_review_pending_on_a_db_built_before_source_keys_is_a_one_line_error(env):
    env.sql("ALTER TABLE facts RENAME COLUMN source_key TO source_key_old")
    r = env.cli("review-pending")
    assert r.returncode == 1 and r.stdout == "" and "rebuild" in r.stderr and r.stderr.count("\n") == 1


# ---------------------------------------------------------------- approve

def test_approve_by_id_appends_a_revision_and_makes_one_commit_of_only_the_log(env):
    before = env.n_commits()
    r = env.cli("approve", "6", "--reason", "checked it")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "approved 6 (p-a)"
    assert r.stdout.splitlines()[1].startswith("Committed ")
    assert "knowledge.py build" in r.stderr  # the db is not rebuilt by approve
    [rev] = env.log()
    assert (rev["source_key"], rev["status"], rev["change_reason"], rev["changed_via"]) == ("p-a", "active", "checked it", "cli")
    assert env.n_commits() == before + 1 and env.head_files() == ["fact_revisions.jsonl"]
    assert git(env.data_dir, "status", "--porcelain") == ""


def test_approve_by_source_key_default_reason_and_json(env):
    r = env.cli("approve", "p-b", "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["ok"] is True and d["errors"] == [] and d["commit"] and d["commit_error"] is None
    [item] = d["items"]
    assert (item["ref"], item["outcome"], item["source_key"]) == ("p-b", "approved", "p-b")
    assert item["revision"]["status"] == "active" and item["revision"]["change_reason"] == "approved"


def test_approve_all_covers_every_pending_fact_including_a_private_subject(env):
    r = env.cli("approve", "--all")
    assert r.returncode == 0, r.stderr
    assert {x["source_key"] for x in env.log()} == {"p-a", "p-b", "p-diary"}
    assert env.n_commits() == 2 and env.head_files() == ["fact_revisions.jsonl"]
    msg = git(env.data_dir, "log", "-1", "--format=%B")
    assert "Secret diary line" not in msg  # the commit message names source_keys, never statements
    again = env.cli("approve", "--all")  # second run: nothing pending, so no commit
    assert again.returncode == 0 and again.stdout.startswith("Nothing to do")
    assert env.n_commits() == 2


def test_approving_twice_is_a_skip_not_a_failure(env):
    ok(env.cli("approve", "6"))
    r = env.cli("approve", "6", "p-a", "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert [i["outcome"] for i in d["items"]] == ["skipped", "skipped"] and d["ok"] is True and d["commit"] is None
    assert len(env.log()) == 1 and env.n_commits() == 2


def test_approve_active_fact_by_id_is_skipped(env):
    r = env.cli("approve", "9")
    assert r.returncode == 0
    assert "skipped 9: status is 'active', not pending" in r.stdout and env.log() == []


def test_db_is_not_rebuilt_so_review_pending_still_lists_it_until_build(env):
    ok(env.cli("approve", "6"))
    assert "#6 " in ok(env.cli("review-pending"))  # documented: run `knowledge.py build` to refresh


@pytest.mark.parametrize("ref", ["999", "no-such-key", "1", "0", "' OR 1=1 --", "１"])
def test_unknown_or_unresolvable_refs_exit_1_and_write_nothing(env, ref):
    r = env.cli("approve", ref)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert env.log() == [] and env.n_commits() == 1
    assert r.stderr.startswith("unknown ") or r.stderr.startswith("error ")
    assert "Traceback" not in r.stderr


def test_mixed_batch_writes_the_good_items_and_still_exits_1(env):
    r = env.cli("approve", "6", "nope", "p-b")
    assert r.returncode == 1
    assert {x["source_key"] for x in env.log()} == {"p-a", "p-b"} and env.n_commits() == 2
    assert "unknown nope" in r.stderr


def test_blank_ref_is_reported_not_a_traceback(env):
    r = env.cli("approve", "   ")
    assert r.returncode == 1 and "blank reference" in r.stderr and "Traceback" not in r.stderr


def test_usage_errors_exit_2(env):
    assert env.cli("approve").returncode == 2
    assert env.cli("approve", "6", "--all").returncode == 2
    assert env.cli("reject", "6").returncode == 2  # --reason is required
    assert env.cli("reject", "--reason", "x").returncode == 2  # and so is a ref
    assert env.log() == []


def test_blank_reason_is_a_batch_error(env):
    r = env.cli("approve", "6", "--reason", "  ")
    assert r.returncode == 1 and "reason is required" in r.stderr and env.log() == []
    r = env.cli("reject", "6", "--reason", "")
    assert r.returncode == 1 and env.log() == []


def test_a_fact_rejected_after_listing_is_never_reactivated(env):
    listed = json.loads(ok(env.cli("review-pending", "--json")))
    assert any(r["id"] == 6 for r in listed)
    ok(env.cli("reject", "6", "--reason", "wrong"))  # someone else retracts it after the listing
    r = env.cli("approve", "6", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["items"][0]["outcome"] == "skipped"
    assert [x["status"] for x in env.log()] == ["retracted"]


def test_without_a_db_source_keys_work_and_numeric_ids_fail(tmp_path):
    e = Env(tmp_path, with_db=False)
    r = e.cli("approve", "6")
    assert r.returncode == 1 and "need a database" in r.stderr and e.log() == []
    r = e.cli("approve", "p-a")
    assert r.returncode == 0, r.stderr
    assert [x["source_key"] for x in e.log()] == ["p-a"]


# ---------------------------------------------------------------- reject

def test_reject_appends_a_retracted_revision(env):
    r = env.cli("reject", "6", "p-b", "--reason", "not true")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[:2] == ["rejected 6 (p-a)", "rejected p-b"]
    assert [(x["source_key"], x["status"], x["change_reason"]) for x in env.log()] == \
        [("p-a", "retracted", "not true"), ("p-b", "retracted", "not true")]
    assert env.n_commits() == 2 and env.head_files() == ["fact_revisions.jsonl"]


def test_reject_json_and_skips_non_pending(env):
    r = env.cli("reject", "9", "p-a", "--reason", "x", "--json")
    assert r.returncode == 0
    d = json.loads(r.stdout)
    assert [i["outcome"] for i in d["items"]] == ["skipped", "rejected"]


# ---------------------------------------------------------------- git safety

def test_dirty_tree_is_refused_up_front_and_nothing_is_written(env):
    env.make_dirty()
    r = env.cli("approve", "6", "--json")
    assert r.returncode == 1
    d = json.loads(r.stdout)
    assert d["ok"] is False and d["errors"] and d["commit"] is None
    assert env.log() == [] and env.n_commits() == 1
    r = env.cli("approve", "6")
    assert r.returncode == 1 and r.stderr.startswith("error: ") and r.stdout == ""


def test_allow_dirty_proceeds_and_commits_only_the_log(env):
    env.make_dirty()
    r = env.cli("approve", "6", "--allow-dirty")
    assert r.returncode == 0, r.stderr
    assert env.head_files() == ["fact_revisions.jsonl"]
    assert git(env.data_dir, "status", "--porcelain") == "?? stray.txt"  # the stray file stays untouched
    assert env.cli("reject", "p-b", "--reason", "x", "--allow-dirty").returncode == 0


def test_a_dirty_tree_does_not_matter_when_there_is_nothing_to_approve(env):
    env.make_dirty()
    assert env.cli("approve", "9").returncode == 0  # only skipped items


def test_commit_failure_exits_3_and_the_revision_stays_written(env):
    hook = os.path.join(env.data_dir, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
    r = env.cli("approve", "6")
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "NOT committed" in r.stderr and "hook says no" in r.stderr
    assert [x["source_key"] for x in env.log()] == ["p-a"] and env.n_commits() == 1
    r = env.cli("reject", "p-b", "--reason", "x", "--allow-dirty", "--json")  # tree is dirty from the first write
    assert r.returncode == 3
    d = json.loads(r.stdout)
    assert d["ok"] is False and d["commit_error"] and d["items"][0]["outcome"] == "rejected"


def test_data_dir_outside_a_git_repo_writes_without_committing(tmp_path):
    e = Env(tmp_path, repo=False)
    r = e.cli("approve", "6")
    assert r.returncode == 0, r.stderr
    assert "not inside a git repository" in r.stderr and "Committed" not in r.stdout
    assert [x["source_key"] for x in e.log()] == ["p-a"]


# ---------------------------------------------------------------- nobody may drop a review result

def test_no_caller_discards_the_result_of_review_approve_or_reject():
    """A refused / skipped / unknown / commit-failed outcome can be silently ignored by a caller that
    treats "did not raise" as "succeeded". Every call to review.approve / review.reject in the
    repo's modules must use its result (not be a bare expression statement)."""
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py") or name == "review.py":
            continue
        with open(os.path.join(root, name)) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = node.value.func
                if isinstance(fn, ast.Attribute) and fn.attr in ("approve", "reject") \
                        and isinstance(fn.value, ast.Name) and fn.value.id == "review":
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == []


def test_the_scan_catches_a_discarded_result(tmp_path):
    import ast
    tree = ast.parse("import review\nreview.approve(['x'])\nres = review.reject(['y'], 'r')\n")
    bare = [n for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert len(bare) == 1 and bare[0].value.func.attr == "approve"
