"""CLI wiring for privacy (#31): `privacy check|rules|tag|untag|add-keyword|remove-keyword`, and
add-fact printing why a fact was stored stricter than requested. Library behavior is in test_privacy.
Temp dirs and temp git repos only (GIT_CONFIG_GLOBAL=/dev/null); never the real knowledge-private.
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_cli_wiring import GIT_ENV, Env, ok  # noqa: E402


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def put_rules(env, **rules):
    os.makedirs(env.data_dir, exist_ok=True)
    with open(env.rules_file(), "w") as f:
        json.dump({"version": 1, **rules}, f)


def entries_last(env):
    with open(os.path.join(env.data_dir, "general_facts.json")) as f:
        return json.load(f)[-1]


# ====================================================================== privacy check / rules

def test_check_no_rules_known_subject_is_normal(env):
    r = env.cli("privacy", "check", "Creatine is effective", "--subject", "nutrition")
    assert (r.returncode, r.stdout, r.stderr) == (0, "normal: no privacy rule applies\n", "")


def test_check_unknown_subject_fails_closed(env):
    out = ok(env.cli("privacy", "check", "x", "--subject", "brand-new"))
    assert out.startswith("private: ") and "unknown-subject" in out and "'brand-new'" in out


def test_check_blank_subject_fails_closed(env):
    out = ok(env.cli("privacy", "check", "x", "--subject", "  "))
    assert out.startswith("private: ") and "no subject" in out


def test_check_subject_tag_and_inheritance_use_the_db_tree(env):
    put_rules(env, subject_tags={"nutrition": "private"})
    assert "subject 'nutrition' is tagged private" in ok(env.cli("privacy", "check", "x", "--subject", "nutrition"))
    out = ok(env.cli("privacy", "check", "x", "--subject", "protein"))  # protein's parent is nutrition
    assert "subject 'protein' is under 'nutrition', which is tagged private" in out
    assert ok(env.cli("privacy", "check", "x", "--subject", "jazz")) == "normal: no privacy rule applies\n"


def test_check_keyword_whole_word_case_insensitive(env):
    put_rules(env, keywords=["mary jane"])
    assert "listed word 'mary jane'" in ok(env.cli("privacy", "check", "I met MARY   Jane, today", "--subject", "jazz"))
    assert ok(env.cli("privacy", "check", "Maryjane is a plant", "--subject", "jazz")).startswith("normal")


def test_check_keyword_with_regex_metacharacters_is_literal(env):
    put_rules(env, keywords=["a.b", "c++", "x|y"])
    assert ok(env.cli("privacy", "check", "axb", "--subject", "jazz")).startswith("normal")
    assert ok(env.cli("privacy", "check", "see a.b here", "--subject", "jazz")).startswith("private")
    assert ok(env.cli("privacy", "check", "I write c++ daily", "--subject", "jazz")).startswith("private")
    assert ok(env.cli("privacy", "check", "x", "--subject", "jazz")).startswith("normal")
    assert ok(env.cli("privacy", "check", "p x|y q", "--subject", "jazz")).startswith("private")


def test_check_requested_private_and_normal(env):
    out = ok(env.cli("privacy", "check", "x", "--subject", "jazz", "--requested", "private"))
    assert out == "private: requested: the caller asked for private\n"
    assert ok(env.cli("privacy", "check", "x", "--subject", "jazz", "--requested", "normal")).startswith("normal")
    assert env.cli("privacy", "check", "x", "--subject", "jazz", "--requested", "secret").returncode == 2


def test_check_json(env):
    put_rules(env, keywords=["coltrane"])
    d = json.loads(ok(env.cli("privacy", "check", "Coltrane played", "--subject", "jazz", "--json")))
    assert d["visibility"] == "private" and d["raised_above_request"] is True
    assert d["reasons"] == [{"kind": "keyword", "detail": "the statement contains the listed word 'coltrane'", "forces_private": True}]
    assert d["explanation"].startswith("private: keyword:")
    d = json.loads(ok(env.cli("privacy", "check", "x", "--subject", "jazz", "--json")))
    assert d["visibility"] == "normal" and d["reasons"] == [] and d["raised_above_request"] is False


def test_check_without_a_db_uses_empty_context(tmp_path):
    e = Env(tmp_path, with_db=False)
    # no subject list to compare against, so the unknown-subject rule cannot fire
    assert ok(e.cli("privacy", "check", "x", "--subject", "anything")) == "normal: no privacy rule applies\n"
    put_rules(e, subject_tags={"anything": "private"})
    assert ok(e.cli("privacy", "check", "x", "--subject", "anything")).startswith("private")


def test_check_knows_subjects_already_in_general_facts_json(env):
    """Same notion of 'known' as add-fact: a subject added but not yet built is not unknown."""
    os.makedirs(env.data_dir, exist_ok=True)
    with open(os.path.join(env.data_dir, "general_facts.json"), "w") as f:
        json.dump([{"subject": "just-added", "statement": "s"}], f)
    assert ok(env.cli("privacy", "check", "x", "--subject", "just-added")).startswith("normal")


def test_check_agrees_with_what_add_fact_stores(env):
    put_rules(env, keywords=["secretname"])
    for statement, subject in (("about secretname", "nutrition"), ("plain", "nutrition"), ("plain", "nope-subject")):
        expected = json.loads(ok(env.cli("privacy", "check", statement, "--subject", subject, "--json")))["visibility"]
        got = env.cli("add-fact", statement, "--subject", subject, "--trust", "medium", "--volatility", "static", "--visibility", "normal")
        assert got.returncode == 0, got.stderr
        assert entries_last(env)["visibility"] == expected, (statement, subject)


def test_check_corrupt_rules_is_a_one_line_error(env):
    os.makedirs(env.data_dir, exist_ok=True)
    with open(env.rules_file(), "w") as f:
        f.write("{not json")
    r = env.cli("privacy", "check", "x", "--subject", "jazz")
    assert r.returncode == 1 and r.stdout == "" and r.stderr.startswith("error: ") and r.stderr.count("\n") == 1


def test_rules_empty_text_and_json(env):
    out = ok(env.cli("privacy", "rules"))
    assert "no rules file" in out and "subject tags: (none)" in out and "keywords: (none)" in out
    d = json.loads(ok(env.cli("privacy", "rules", "--json")))
    assert d == {"path": env.rules_file(), "exists": False, "version": 1, "subject_tags": {}, "keywords": []}


def test_rules_lists_loaded_rules(env):
    put_rules(env, subject_tags={"family": "private", "newtopic": "normal"}, keywords=["Zed"])
    out = ok(env.cli("privacy", "rules"))
    assert "family: private" in out and "newtopic: normal" in out and "- zed" in out
    d = json.loads(ok(env.cli("privacy", "rules", "--json")))
    assert d["exists"] is True and d["keywords"] == ["zed"] and d["subject_tags"]["family"] == "private"


def test_rules_corrupt_file_errors(env):
    put_rules(env, bogus_key=1)
    r = env.cli("privacy", "rules")
    assert r.returncode == 1 and r.stderr.startswith("error: ") and "bogus_key" in r.stderr


# ====================================================================== privacy edit commands

def git(cwd, *args, check=True):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check:
        assert p.returncode == 0, p.stderr
    return p.stdout.strip()


class GitEnv(Env):
    """Env whose data dir lives inside a git repo (knowledge-private) with one initial commit."""
    def __init__(self, root):
        super().__init__(root)
        self.data_dir = os.path.join(self.private, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.private, "local_paths.py"), "w") as f:
            f.write(f"KNOWLEDGE_DB_DIR = {self.db_dir!r}\nPRIVATE_DATA_DIR = {self.data_dir!r}\n")
            for n in ("BODYBUILDING_VAULT", "HEALTH_DIR", "CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON"):
                f.write(f"{n} = {os.path.join(self.root, 'unused')!r}\n")
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


@pytest.fixture
def g(tmp_path):
    return GitEnv(tmp_path)


def test_tag_creates_file_on_first_use_and_commits_only_it(g):
    assert not os.path.exists(g.rules_file())
    r = g.cli("privacy", "tag", "family")
    assert r.returncode == 0, r.stderr
    assert g.rules()["subject_tags"] == {"family": "private"}
    assert g.status() == ""
    assert g.log()[0] == "privacy: tag family (private)"
    assert git(g.private, "show", "--name-only", "--format=", "HEAD") == "data/privacy_rules.json"
    assert "Committed " in r.stdout


def test_each_edit_command_commits_with_its_own_message(g):
    for args, msg in ((("tag", "family"), "privacy: tag family (private)"),
                      (("add-keyword", "Zed Name"), "privacy: add keyword 'zed name'"),
                      (("remove-keyword", "zed  NAME"), "privacy: remove keyword 'zed name'"),
                      (("untag", "family"), "privacy: untag family")):
        assert g.cli("privacy", *args).returncode == 0
        assert g.log()[0] == msg
    assert g.rules() == {"version": 1, "subject_tags": {}, "keywords": []}
    assert g.status() == ""


def test_tag_normal_registers_a_subject(g):
    assert g.cli("privacy", "tag", "newtopic", "--tag", "normal").returncode == 0
    assert g.rules()["subject_tags"] == {"newtopic": "normal"}
    assert g.log()[0] == "privacy: tag newtopic (normal)"


def test_edit_that_changes_nothing_is_idempotent_with_no_empty_commit(g):
    assert g.cli("privacy", "tag", "family").returncode == 0
    assert g.cli("privacy", "add-keyword", "Zed").returncode == 0
    head = g.head()
    for args in (("tag", "family"), ("untag", "never-tagged"), ("remove-keyword", "absent"), ("add-keyword", "  ZED ")):
        r = g.cli("privacy", *args)
        assert r.returncode == 0 and "no change" in r.stdout and "Committed" not in r.stdout, (args, r.stderr)
    assert g.head() == head and g.status() == ""


def test_no_change_on_a_dirty_tree_is_fine_but_a_real_edit_is_refused(g):
    assert g.cli("privacy", "tag", "family").returncode == 0
    with open(os.path.join(g.data_dir, "other.json"), "w") as f:
        f.write('["edited"]\n')
    assert g.cli("privacy", "tag", "family").returncode == 0  # nothing to write, nothing to mix up
    before = open(g.rules_file()).read()
    head = g.head()
    r = g.cli("privacy", "tag", "work")
    assert r.returncode == 1
    assert "uncommitted changes" in r.stderr and "other.json" in r.stderr and "--allow-dirty" in r.stderr
    assert open(g.rules_file()).read() == before and g.head() == head


def test_untracked_file_in_the_repo_refuses_a_real_edit(g):
    with open(os.path.join(g.private, "scratch.txt"), "w") as f:
        f.write("x")
    r = g.cli("privacy", "add-keyword", "zed")
    assert r.returncode == 1 and "scratch.txt" in r.stderr
    assert not os.path.exists(g.rules_file())


def test_allow_dirty_commits_only_the_rules_file(g):
    other = os.path.join(g.data_dir, "other.json")
    with open(other, "w") as f:
        f.write('["staged"]\n')
    git(g.private, "add", other)
    with open(os.path.join(g.private, "scratch.txt"), "w") as f:
        f.write("x")
    r = g.cli("privacy", "add-keyword", "zed", "--allow-dirty")
    assert r.returncode == 0, r.stderr
    assert git(g.private, "show", "--name-only", "--format=", "HEAD") == "data/privacy_rules.json"
    assert sorted(g.status().splitlines()) == sorted(["M  data/other.json", "?? scratch.txt"])


@pytest.mark.parametrize("args", [("tag", "family"), ("add-keyword", "zed")])
def test_dry_run_writes_nothing_and_commits_nothing(g, args):
    head = g.head()
    r = g.cli("privacy", *args, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "dry run" in r.stdout.lower() and "privacy:" in r.stdout
    assert not os.path.exists(g.rules_file()) and g.head() == head and g.status() == ""


def test_dry_run_with_no_change_says_so(g):
    assert g.cli("privacy", "tag", "family").returncode == 0
    r = g.cli("privacy", "tag", "family", "--dry-run")
    assert r.returncode == 0 and "no change" in r.stdout


def test_dry_run_on_a_dirty_tree_reports_the_refusal(g):
    with open(os.path.join(g.private, "scratch.txt"), "w") as f:
        f.write("x")
    r = g.cli("privacy", "tag", "family", "--dry-run")
    assert r.returncode == 1 and "scratch.txt" in r.stderr


def test_data_dir_outside_any_git_repo_writes_with_a_note(env):
    r = env.cli("privacy", "tag", "family")  # base Env: data dir is a plain temp dir (does not exist yet)
    assert r.returncode == 0, r.stderr
    assert "not inside a git repository" in r.stderr and "Committed" not in r.stdout
    assert env.rules()["subject_tags"] == {"family": "private"}


def test_edits_change_what_check_says(g):
    assert g.cli("privacy", "tag", "nutrition").returncode == 0
    assert ok(g.cli("privacy", "check", "x", "--subject", "protein")).startswith("private")
    assert g.cli("privacy", "untag", "nutrition").returncode == 0
    assert ok(g.cli("privacy", "check", "x", "--subject", "protein")).startswith("normal")
    assert g.cli("privacy", "add-keyword", "c++").returncode == 0
    assert ok(g.cli("privacy", "check", "I use c++", "--subject", "jazz")).startswith("private")


@pytest.mark.parametrize("args", [
    ("tag", "Not Kebab"), ("tag", ""), ("tag", "  "), ("add-keyword", ""), ("add-keyword", "   "),
    ("add-keyword", "((("), ("add-keyword", "a\x01b"), ("remove-keyword", "")])
def test_invalid_edits_are_one_line_errors_and_write_nothing(g, args):
    head = g.head()
    r = g.cli("privacy", *args)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert r.stderr.startswith("error: ") and r.stderr.count("\n") == 1 and "Traceback" not in r.stderr
    assert not os.path.exists(g.rules_file()) and g.head() == head and g.status() == ""


def test_keyword_with_metacharacters_is_stored_literally(g):
    assert g.cli("privacy", "add-keyword", "a.b*c").returncode == 0
    assert g.rules()["keywords"] == ["a.b*c"]


def test_corrupt_rules_file_is_left_untouched_by_an_edit(g):
    with open(g.rules_file(), "w") as f:
        f.write("{oops")
    git(g.private, "add", "-A")
    git(g.private, "commit", "-q", "-m", "corrupt")
    r = g.cli("privacy", "tag", "family")
    assert r.returncode == 1 and r.stderr.startswith("error: ")
    assert open(g.rules_file()).read() == "{oops" and g.status() == ""


def _block_commits(g):
    hook = os.path.join(g.private, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\necho blocked >&2\nexit 1\n")
    os.chmod(hook, 0o755)


def test_failed_commit_exits_3_and_says_the_change_is_written_but_uncommitted(g):
    _block_commits(g)
    r = g.cli("privacy", "tag", "family")
    assert r.returncode == 3
    assert "NOT committed" in r.stderr and "blocked" in r.stderr
    assert g.rules()["subject_tags"] == {"family": "private"}


def test_second_attempt_after_a_failed_commit_needs_allow_dirty(g):
    _block_commits(g)
    assert g.cli("privacy", "tag", "family").returncode == 3
    r = g.cli("privacy", "add-keyword", "zed")
    assert r.returncode == 1 and "uncommitted changes" in r.stderr
    assert g.rules()["keywords"] == []


# ====================================================================== add-fact prints the privacy explanation

def test_add_fact_notes_when_visibility_was_raised(g):
    assert g.cli("privacy", "add-keyword", "secretname").returncode == 0
    r = g.cli("add-fact", "About secretname.", "--subject", "nutrition", "--trust", "medium", "--volatility", "static", "--visibility", "normal")
    assert r.returncode == 0, r.stderr
    assert "stored as private" in r.stderr and "secretname" in r.stderr
    assert entries_last(g)["visibility"] == "private"


def test_add_fact_silent_when_nothing_raised_it(g):
    r = g.cli("add-fact", "Plain statement.", "--subject", "nutrition", "--trust", "medium", "--volatility", "static", "--visibility", "normal")
    assert r.returncode == 0, r.stderr
    assert "stored as private" not in r.stderr
    assert entries_last(g)["visibility"] == "normal"


def test_add_fact_silent_when_private_was_requested(g):
    assert g.cli("privacy", "add-keyword", "secretname").returncode == 0
    r = g.cli("add-fact", "About secretname.", "--subject", "nutrition", "--trust", "medium", "--volatility", "static")  # default: private
    assert r.returncode == 0 and "stored as private" not in r.stderr
