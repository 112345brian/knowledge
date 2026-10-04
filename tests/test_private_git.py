"""Issue #10: auto-commit of knowledge-private mutations with a clean-tree precondition.

Every test uses a throwaway git repo as the private dir, with global/system git config
disabled so the developer's own identity, hooks and aliases cannot affect the result.
"""
import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_add_fact import Env  # noqa: E402
import private_git  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check:
        assert p.returncode == 0, p.stderr
    return p.stdout.strip()


class GitEnv(Env):
    """Env whose private dir is a git repo with the data dir inside it, one initial commit."""
    def __init__(self, root):
        super().__init__(root)
        self.data_dir = os.path.join(self.private, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.private, "local_paths.py"), "w") as f:
            f.write(f"KNOWLEDGE_DB_DIR = {self.db_dir!r}\nPRIVATE_DATA_DIR = {self.data_dir!r}\n")
            for n in ("BODYBUILDING_VAULT", "HEALTH_DIR", "CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON"):
                f.write(f"{n} = {os.path.join(self.root, 'unused')!r}\n")
        self.facts = os.path.join(self.data_dir, "general_facts.json")
        self.env = {**self.env, **GIT_ENV}
        git(self.private, "init", "-q")
        git(self.private, "config", "user.name", "Test")
        git(self.private, "config", "user.email", "test@example.com")
        with open(os.path.join(self.data_dir, "other.json"), "w") as f:
            f.write("[]\n")
        git(self.private, "add", "-A")
        git(self.private, "commit", "-q", "-m", "init")

    def head(self):
        return git(self.private, "rev-parse", "HEAD")

    def status(self):
        return git(self.private, "status", "--porcelain", "--untracked-files=all")

    def log(self):
        return git(self.private, "log", "--format=%s").splitlines()

    def add(self, text="A fact.", subject="car-maintenance", trust="medium", *extra):
        return self.cli(text, "--subject", subject, "--trust", trust, *extra)


@pytest.fixture
def g(tmp_path):
    return GitEnv(tmp_path)


# ---------------------------------------------------------------- add_fact CLI end to end

def test_add_commits_and_leaves_tree_clean(g):
    r = g.add()
    assert r.returncode == 0, r.stderr
    assert g.status() == ""
    assert g.log()[0] == "add-fact: car-maintenance (medium)"
    assert git(g.private, "show", "--name-only", "--format=", "HEAD") == "data/general_facts.json"
    assert "Committed " in r.stdout
    assert len(g.entries()) == 1


def test_second_add_right_after_the_first_makes_a_second_commit(g):
    assert g.add("One.", "alpha", "high").returncode == 0
    assert g.add("Two.", "beta", "low").returncode == 0
    assert g.log()[:2] == ["add-fact: beta (low)", "add-fact: alpha (high)"]
    assert g.status() == ""
    assert len(g.entries()) == 2


def test_dirty_tracked_file_is_refused_and_nothing_is_written(g):
    with open(os.path.join(g.data_dir, "other.json"), "w") as f:
        f.write('["edited"]\n')
    head = g.head()
    r = g.add()
    assert r.returncode == 1
    assert "uncommitted changes" in r.stderr and "other.json" in r.stderr and "--allow-dirty" in r.stderr
    assert not os.path.exists(g.facts)
    assert g.head() == head


def test_untracked_file_is_refused(g):
    with open(os.path.join(g.private, "scratch.txt"), "w") as f:
        f.write("x")
    r = g.add()
    assert r.returncode == 1 and "scratch.txt" in r.stderr
    assert not os.path.exists(g.facts)


def test_staged_change_is_refused(g):
    p = os.path.join(g.data_dir, "other.json")
    with open(p, "w") as f:
        f.write('["staged"]\n')
    git(g.private, "add", p)
    r = g.add()
    assert r.returncode == 1 and "uncommitted changes" in r.stderr
    assert not os.path.exists(g.facts)


def test_ignored_files_do_not_count_as_dirty(g):
    with open(os.path.join(g.private, ".gitignore"), "w") as f:
        f.write("*.log\n")
    git(g.private, "add", ".gitignore")
    git(g.private, "commit", "-q", "-m", "ignore")
    with open(os.path.join(g.private, "noise.log"), "w") as f:
        f.write("x")
    assert g.add().returncode == 0


def test_allow_dirty_commits_only_the_facts_file(g):
    other = os.path.join(g.data_dir, "other.json")
    with open(other, "w") as f:
        f.write('["staged"]\n')
    git(g.private, "add", other)
    with open(os.path.join(g.private, "scratch.txt"), "w") as f:
        f.write("x")
    r = g.add("Deliberate.", "gamma", "low", "--allow-dirty")
    assert r.returncode == 0, r.stderr
    assert git(g.private, "show", "--name-only", "--format=", "HEAD") == "data/general_facts.json"
    assert sorted(g.status().splitlines()) == sorted(["M  data/other.json", "?? scratch.txt"])


def test_validation_failure_in_a_clean_repo_commits_nothing(g):
    head = g.head()
    r = g.add("   ")
    assert r.returncode == 1 and "statement is empty" in r.stderr
    assert g.head() == head and g.status() == "" and not os.path.exists(g.facts)


def test_detached_head_commits_with_a_warning(g):
    git(g.private, "checkout", "-q", "--detach")
    r = g.add()
    assert r.returncode == 0, r.stderr
    assert "detached HEAD" in r.stderr
    assert g.log()[0] == "add-fact: car-maintenance (medium)" and g.status() == ""


def test_failing_pre_commit_hook_is_reported_and_exit_is_nonzero(g):
    hook = os.path.join(g.private, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)
    head = g.head()
    r = g.add()
    assert r.returncode == 3
    assert "IS in" in r.stderr and "NOT committed" in r.stderr and "hook says no" in r.stderr
    assert len(g.entries()) == 1          # the fact is in the file...
    assert g.head() == head               # ...but not committed


def test_missing_git_identity_is_reported(g):
    git(g.private, "config", "--unset", "user.name")
    git(g.private, "config", "--unset", "user.email")
    git(g.private, "config", "user.useConfigOnly", "true")
    r = g.add()
    assert r.returncode == 3 and "NOT committed" in r.stderr
    assert len(g.entries()) == 1


def test_gitignored_facts_file_is_reported_not_swallowed(g):
    with open(os.path.join(g.private, ".gitignore"), "w") as f:
        f.write("general_facts.json\n")
    git(g.private, "add", ".gitignore")
    git(g.private, "commit", "-q", "-m", "ignore facts")
    r = g.add()
    assert r.returncode == 3 and "NOT committed" in r.stderr


def test_data_dir_outside_any_repo_writes_with_an_explicit_note(tmp_path):
    e = Env(tmp_path)
    e.env = {**e.env, **GIT_ENV}
    r = e.cli("Fact.", "--subject", "x", "--trust", "low")
    assert r.returncode == 0
    assert "not inside a git repository" in r.stderr and "Committed" not in r.stdout
    assert len(e.entries()) == 1


def test_knowledge_py_wrapper_propagates_the_refusal(g):
    with open(os.path.join(g.private, "scratch.txt"), "w") as f:
        f.write("x")
    r = g.cli("Fact.", "--subject", "x", "--trust", "low", via="knowledge")
    assert r.returncode == 1 and "uncommitted changes" in r.stderr


def test_append_fact_library_function_has_no_git_side_effects(g):
    code = ("import add_fact; r = add_fact.append_fact(add_fact.NewFact(statement='L.', subject='lib', "
            "trust_level='low')); print(r.ok)")
    p = subprocess.run([sys.executable, "-c", code], env=g.env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       capture_output=True, text=True)
    assert p.stdout.strip() == "True", p.stderr
    assert g.log() == ["init"]
    assert g.status() == "?? data/general_facts.json"


# ---------------------------------------------------------------- helper API (for approve/supersede/...)

def test_helper_ensure_clean_tree_and_find_repo(g, tmp_path):
    assert os.path.realpath(private_git.find_repo(g.data_dir)) == os.path.realpath(g.private)
    assert private_git.find_repo(str(tmp_path / "does-not-exist")) is None
    private_git.ensure_clean_tree(g.private)
    with open(os.path.join(g.private, "x"), "w") as f:
        f.write("1")
    with pytest.raises(private_git.PrivateGitError, match="uncommitted"):
        private_git.ensure_clean_tree(g.private)


def test_helper_commit_returns_hash_and_nothing_to_commit_raises(g):
    with open(g.facts, "w") as f:
        f.write("[]\n")
    h = private_git.commit_private_change([g.facts], "set-visibility: x (high)", g.private)
    assert h and g.log()[0] == "set-visibility: x (high)"
    with pytest.raises(private_git.PrivateGitError, match="git commit failed"):
        private_git.commit_private_change([g.facts], "again", g.private)
    with pytest.raises(private_git.PrivateGitError):
        private_git.commit_private_change([], "no paths", g.private)


def test_helper_many_untracked_files_are_summarised(g):
    for i in range(12):
        with open(os.path.join(g.private, f"f{i}"), "w") as f:
            f.write("x")
    with pytest.raises(private_git.PrivateGitError, match="and 2 more"):
        private_git.ensure_clean_tree(g.private)
