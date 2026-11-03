"""Characterization of every write and fact-read path (increment 1 of docs/db-source-of-truth.md).

When the database becomes the source of truth, each place that writes a private JSON file or reads facts must be moved
deliberately: writes to a database adapter, reads to a logged read. These tests pin today's inventory so that a new
write or read path cannot appear (or an old one be forgotten) without an edit here, and pin what each CLI write
command changes on disk and commits, so the later change can be compared with it. Temp dirs and a temp git repo only.
"""
import ast
import os
import subprocess
import sys

import pytest

from config_files import write_unused_inputs_config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE = os.path.join(REPO, "knowledge.py")

# Calls that write a private data file or commit one. Method names on the write ports (facts_file_port,
# revisions_port, privacy_rules_port, the private_edit_store flow, private_git_port).
WRITE_SEAMS = {"append_record", "append_records", "append_revision", "save_rules", "edit_file", "edit_files",
               "commit_private_change"}
# Calls that read facts for a driving adapter (fact_reads_port): the points where an access would be logged.
READ_SEAMS = {"search_facts", "list_facts", "get_fact", "get_history", "fact_as_of", "list_pending"}

# module -> the seams it calls. A module that gains or loses a call fails the test until this table says so.
WRITERS = {
    "add_fact_service.py": {"append_record", "commit_private_change"},
    "facts_batch_service.py": {"append_records", "commit_private_change"},
    "migrate_memory_service.py": {"commit_private_change"},
    "lifecycle_service.py": {"append_revision", "commit_private_change"},
    "review_service.py": {"append_revision", "commit_private_change"},
    "rules_edit_service.py": {"save_rules", "commit_private_change"},
    "entities_service.py": {"edit_file"},
    "subjects_service.py": {"edit_file"},
    "entities_store.py": {"edit_files"},
    "subjects_store.py": {"edit_files"},
    "entity_migration_store.py": {"save_rules", "edit_files"},
    "private_edit_store.py": {"commit_private_change"},
}
READERS = {
    "queries_service.py": {"search_facts", "list_facts", "get_fact", "get_history", "fact_as_of", "list_pending"},
    "review_service.py": {"list_pending"},
    "mcp_service.py": {"search_facts"},
}


def _called_seams(path, seams):
    """The names in `seams` that this file calls as `x.name(...)` or `name(...)`."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name in seams:
                called.add(name)
    return called


def _scan(seams):
    """{module path relative to the repo: seams called} for every non-test, non-port, non-definition source file."""
    found = {}
    for base, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {"tests", ".venv", "docs", "__pycache__", ".git", ".claude"}]
        for name in files:
            if not name.endswith(".py") or name.endswith("_port.py"):
                continue
            rel = os.path.relpath(os.path.join(base, name), REPO)
            called = _called_seams(os.path.join(base, name), seams)
            if called:
                found[rel] = called
    return found


def test_the_set_of_modules_that_write_private_files_is_pinned():
    found = _scan(WRITE_SEAMS)
    assert {k: v for k, v in found.items() if "/" not in k} == WRITERS, (
        "a module began or stopped writing private data files: it must be moved to the database adapter deliberately "
        "(docs/db-source-of-truth.md, increment 4); update WRITERS only together with that change")


def test_pipeline_and_client_steps_never_call_the_cli_write_seams():
    """Build steps and client tools load files; they never go through the commit-per-write seams."""
    found = _scan({"commit_private_change", "append_record", "append_records", "append_revision"})
    assert [k for k in found if k.startswith(("ingest/", "client/"))] == []


def test_the_set_of_modules_that_read_facts_for_an_adapter_is_pinned():
    found = _scan(READ_SEAMS)
    top = {k: v for k, v in found.items() if "/" not in k}
    # fact_queries / fact_reads_store define the reads and call each other: they are the adapter, not a caller.
    for adapter in ("fact_queries.py", "fact_reads_store.py"):
        top.pop(adapter, None)
    assert top == READERS, (
        "a module began or stopped reading facts: every read must be loggable as an access "
        "(docs/db-source-of-truth.md, Logging); update READERS together with that change")


# ---------------------------------------------------------------- what each CLI write command does today

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class World:
    """A temp data dir that is a git repo, a config pointing at it, and a way to run the CLI."""

    def __init__(self, root):
        self.root = str(root)
        self.data = os.path.join(self.root, "data")
        self.db_dir = os.path.join(self.root, "db")
        for d in (self.data, self.db_dir):
            os.makedirs(d)
        self.config = write_unused_inputs_config(os.path.join(self.root, "config.toml"), self.db_dir, self.data,
                                                 os.path.join(self.root, "unused"))
        self.env = {**os.environ, **GIT_ENV, "KNOWLEDGE_CONFIG": self.config}
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "init")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.data, *args], env=self.env, capture_output=True, text=True,
                              check=True).stdout

    def cli(self, *args):
        return subprocess.run([sys.executable, KNOWLEDGE, *args], env=self.env, cwd=self.root,
                              capture_output=True, text=True)

    def footprint(self):
        """(files the last commit changed, its subject, commits so far)."""
        files = sorted(self.git("show", "--name-only", "--format=", "HEAD").split())
        return files, self.git("log", "-1", "--format=%s").strip(), int(self.git("rev-list", "--count", "HEAD"))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_each_write_command_changes_one_file_and_makes_one_commit(world):
    cases = [
        (("add-fact", "Water boils at 100C at sea level", "--subject", "physics", "--trust", "high", "--no-decay",
          "--recheck-rationale", "constant"),
         (["general_facts.json"], "add-fact: physics (high)")),
        (("privacy", "tag", "physics"), (["privacy_rules.json"], "privacy: tag physics (private)")),
        (("privacy", "add-keyword", "secretword"), (["privacy_rules.json"], "privacy: add keyword 'secretword'")),
        (("entity", "add", "Acme Co", "--type", "organization"), (["entities.json"], "entities: add Acme Co")),
    ]
    commits = 1
    for args, (files, subject) in cases:
        r = world.cli(*args)
        assert r.returncode == 0, r.stderr
        commits += 1
        assert world.footprint() == (files, subject, commits), args
    assert world.git("status", "--porcelain") == ""   # nothing left uncommitted or stray (no db, no lock file)


def test_dry_run_writes_and_commits_nothing(world):
    r = world.cli("privacy", "tag", "physics", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert world.footprint() == ([], "init", 1)
    assert os.listdir(world.data) == [".git"]
    assert os.listdir(world.db_dir) == []   # a write command never creates or touches the database today


def test_a_dirty_data_repo_refuses_a_write(world):
    with open(os.path.join(world.data, "stray.txt"), "w") as f:
        f.write("x")
    r = world.cli("privacy", "tag", "physics")
    assert r.returncode != 0
    assert not os.path.exists(os.path.join(world.data, "privacy_rules.json"))
    assert world.footprint() == ([], "init", 1)
