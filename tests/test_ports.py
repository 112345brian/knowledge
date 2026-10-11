"""Onion architecture: the ports (`*_port.py`) and the application services that depend on them.

* every adapter module really satisfies the protocol its facade hands to a service;
* each service declares its dependencies as the required fields of its own `*Deps` class, and uses exactly those;
* a service runs against in-memory fakes, with no file, db, git or clock (the point of a port);
* a service touches `os` only for os.path string handling.
"""
import ast
import dataclasses
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wiring  # noqa: E402
from appctx import app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = ("review_service", "lifecycle_service", "add_fact_service", "facts_batch_service", "migrate_memory_service",
            "rules_edit_service", "entities_service", "subjects_service", "queries_service", "scripts_service")


def test_composing_every_root_verifies_its_adapters_against_their_ports():
    """compose() calls wiring.verify, so building the whole container is the check: a renamed method, a changed
    parameter or a missing attribute on any adapter raises PortMismatch here, and at every real startup."""
    from locations import Locations
    import container
    loc = Locations(knowledge_db_dir="/x/db", private_data_dir="/x/data", notes_vault="/x/vault", health_dir="/x/h")
    app = container.Container(loc)
    for name in ("review", "lifecycle", "add_fact", "facts_batch", "migrate_memory", "rules_edit", "entities", "subjects",
                 "queries", "scripts"):
        deps = getattr(app, name).deps
        assert dataclasses.is_dataclass(deps)
        assert all(getattr(deps, f.name) is not None for f in dataclasses.fields(deps)), name


def test_verify_rejects_a_wrong_adapter_a_renamed_method_and_an_unknown_port():
    import review_service
    from locations import Locations
    loc = Locations(knowledge_db_dir="/x/db", private_data_dir="/x/data", notes_vault="/x/vault", health_dir="/x/h")

    class Bad:
        def find_repo(self):  # wrong parameters, and the rest is missing
            return None
    problems = wiring.conformance_problems("git", Bad())
    assert any("find_repo" in p for p in problems) and any("ensure_clean_tree is missing" in p for p in problems)
    import private_git, privacy_store, review_store, revisions_store
    ok = dict(git=private_git, revisions=revisions_store, rules=privacy_store, reads=review_store, locations=loc)
    assert wiring.verify(review_service.ReviewDeps(**ok)) is not None
    with pytest.raises(wiring.PortMismatch, match="git"):
        wiring.verify(review_service.ReviewDeps(**{**ok, "git": Bad()}))
    with pytest.raises(wiring.PortMismatch, match="locations"):
        wiring.verify(review_service.ReviewDeps(**{**ok, "locations": "/x"}))

    import dataclasses as dc

    @dc.dataclass(frozen=True)
    class Stray:
        mystery: object
    with pytest.raises(wiring.PortMismatch, match="not a known port"):
        wiring.verify(Stray(mystery=object()))


def test_bind_keeps_the_public_signature_without_ports():
    import inspect

    def fn(ports_, a, b=2):
        """doc"""
        return (ports_, a, b)
    bound = wiring.bind(fn, "P")
    assert bound(1) == ("P", 1, 2) and bound.__doc__ == "doc"
    assert list(inspect.signature(bound).parameters) == ["a", "b"]


class Untouched:
    """A dependency the code under test must not use: any attribute access fails the test."""
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"the service touched {self._name}.{attr}")


class FakeReads:
    def __init__(self, states):
        self.states = states
        self.opened = []

    def default_data_dir(self):
        return "/nowhere"

    def current_states(self, data_dir=None):
        return {k: dict(v) for k, v in self.states.items()}

    def source_key_for_id(self, db, fact_id):
        return False, None


class FakeRevisions:
    def __init__(self):
        self.calls = []

    def append_revision(self, key, changes, reason, via, session_id=None, data_dir=None, revisions_path=None, expect=None):
        import revisions
        self.calls.append((key, changes, reason, expect))
        return revisions.RevisionResult(True, revision={"source_key": key, **changes})


def test_review_service_approves_against_in_memory_fakes():
    """No tmp dir, no git repo, no db: the use case only needs its ports."""
    import review_service
    reads = FakeReads({"k1": {"status": "pending"}, "k2": {"status": "active"}})
    fake_rev = FakeRevisions()
    p = review_service.ReviewDeps(git=Untouched("git"), revisions=fake_rev, rules=Untouched("rules"), reads=reads,
                                  locations=_fake_locations())
    result = review_service.approve(p, ["k1", "k2", "nope"], reason="ok", commit=False)
    assert [(i.ref, i.outcome) for i in result.items] == [("k1", "approved"), ("k2", "skipped"), ("nope", "unknown")]
    assert fake_rev.calls == [("k1", {"status": "active"}, "ok", {"status": "pending"})]


def test_review_service_reports_a_failed_write_per_item():
    import review_service
    import revisions

    class Failing(FakeRevisions):
        def append_revision(self, key, *a, **k):
            return revisions.RevisionResult(False, ["disk on fire"])
    p = review_service.ReviewDeps(git=Untouched("git"), revisions=Failing(), rules=Untouched("rules"),
                                  reads=FakeReads({"k1": {"status": "pending"}}), locations=_fake_locations())
    (item,) = review_service.approve(p, ["k1"], commit=False).items
    assert item.outcome == "error" and "disk on fire" in item.reason


def _service_trees():
    for name in SERVICES + ("wiring",):
        path = os.path.join(REPO, name + ".py")
        yield name, ast.parse(open(path, encoding="utf-8").read())


def test_use_cases_touch_os_only_for_path_strings():
    offenders = []
    for name, tree in _service_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                continue
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) \
                    and isinstance(node.value.value, ast.Name) and node.value.value.id == "os" and node.value.attr == "path":
                if node.attr not in {"join", "basename", "dirname", "splitext"}:
                    offenders.append(f"{name}.py:{node.lineno}: os.path.{node.attr} reads the file system")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" \
                    and node.attr != "path":
                offenders.append(f"{name}.py:{node.lineno}: os.{node.attr} is not a path-string helper")
    assert offenders == [], offenders


def test_use_cases_do_not_print_open_files_or_exit():
    offenders = []
    for name, tree in _service_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"print", "open", "exit", "input"}:
                offenders.append(f"{name}.py:{node.lineno}: {node.func.id}()")
    assert offenders == [], offenders


DEPS_CLASSES = {"review_service": "ReviewDeps", "lifecycle_service": "LifecycleDeps", "add_fact_service": "AddFactDeps",
                "facts_batch_service": "FactsBatchDeps", "migrate_memory_service": "MigrateMemoryDeps",
                "rules_edit_service": "RulesEditDeps", "entities_service": "EntitiesDeps", "subjects_service": "SubjectsDeps",
                "queries_service": "QueriesDeps", "scripts_service": "ScriptsDeps"}


def test_every_service_has_a_frozen_deps_class_with_only_required_ports():
    assert set(DEPS_CLASSES) == set(SERVICES)
    for module, cls_name in DEPS_CLASSES.items():
        cls = getattr(importlib.import_module(module), cls_name)
        assert dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen, cls_name
        for f in dataclasses.fields(cls):
            assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING, f"{cls_name}.{f.name} is optional"
            assert f.name == "locations" or f.name in wiring.PORT_PROTOCOLS, f"{cls_name}.{f.name} is not a known port"


def test_a_service_uses_exactly_the_dependencies_it_declares():
    """`deps.x` in a service must name a field of its Deps class, and every field must be used somewhere:
    a declared-but-unused port is a wiring lie, and an undeclared one fails only when that path runs."""
    for module, cls_name in DEPS_CLASSES.items():
        declared = {f.name for f in dataclasses.fields(getattr(importlib.import_module(module), cls_name))}
        tree = ast.parse(open(os.path.join(REPO, module + ".py"), encoding="utf-8").read())
        used = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "deps"}
        assert used <= declared, f"{module} uses undeclared dependencies: {sorted(used - declared)}"
        assert declared <= used, f"{module} declares unused dependencies: {sorted(declared - used)}"


class FakeGit:
    class PrivateGitError(Exception):
        pass

    def __init__(self, repo="/repo", dirty=False, commit_fails=False, detached=False):
        self.repo, self.dirty, self.commit_fails, self.detached = repo, dirty, commit_fails, detached
        self.commits = []

    def find_repo(self, directory):
        return self.repo

    def ensure_clean_tree(self, repo_dir):
        if self.dirty:
            raise self.PrivateGitError("working tree is dirty")

    def commit_private_change(self, paths, message, repo_dir):
        if self.commit_fails:
            raise self.PrivateGitError("index.lock exists")
        self.commits.append((list(paths), message))
        return "abc1234"

    def is_detached(self, repo_dir):
        return self.detached


class FakeFactsFile:
    def __init__(self):
        self.records = []

    def citekey_problem(self, db_path, citekey):
        return None, None

    def subject_tree(self, db_path):
        return {}, None

    def file_subjects(self, data_path):
        return set()

    def data_dir_of(self, data_path):
        return os.path.dirname(data_path)

    def file_sha256(self, path):
        return None

    def canonical_subject(self, data_path, subject):
        return subject, [], None

    def append_record(self, path, record):
        self.records.append(record)
        return len(self.records)


class FakeRules:
    def load_rules(self, path):
        import privacy
        return privacy.parse_rules({})

    def rules_path(self):
        return "/nowhere/privacy_rules.json"


class FakeClock:
    def now_iso(self):
        return "2026-01-01T00:00:00+00:00"


class FakeIds:
    def new_source_key(self):
        return "f-000000000001"


def _fake_locations():
    from locations import Locations
    return Locations(knowledge_db_dir="/data", private_data_dir="/data", notes_vault="/vault", health_dir="/health")


def _record_deps(git):
    import add_fact_service
    files = FakeFactsFile()
    return add_fact_service.AddFactDeps(facts_file=files, rules=FakeRules(), clock=FakeClock(), ids=FakeIds(),
                                        locations=_fake_locations(), git=git), files


def _fact(**kw):
    import new_fact
    base = dict(statement="A fact.", subject="x", trust_level="low", no_decay=True, recheck_rationale="no decay")
    base.update(kw)
    return new_fact.NewFact(**base)


def test_record_fact_commits_only_the_facts_file_in_a_clean_repo():
    import add_fact_service
    git = FakeGit()
    deps, files = _record_deps(git)
    out = add_fact_service.record_fact(deps, _fact())
    assert out.ok and out.commit == "abc1234" and out.repo == "/repo" and not out.detached
    assert git.commits == [(["/data/general_facts.json"], "add-fact: x (low)")]
    assert len(files.records) == 1


def test_record_fact_refuses_a_dirty_tree_before_writing_anything():
    import add_fact_service
    git = FakeGit(dirty=True)
    deps, files = _record_deps(git)
    out = add_fact_service.record_fact(deps, _fact())
    assert not out.ok and out.refused == "working tree is dirty" and out.added is None
    assert files.records == [] and git.commits == []
    # --allow-dirty is the only way past it
    out = add_fact_service.record_fact(deps, _fact(), allow_dirty=True)
    assert out.ok and len(files.records) == 1


def test_record_fact_writes_without_committing_outside_a_git_repo():
    import add_fact_service
    git = FakeGit(repo=None)
    deps, files = _record_deps(git)
    out = add_fact_service.record_fact(deps, _fact())
    assert out.ok and out.repo is None and out.commit is None and git.commits == []
    assert len(files.records) == 1


def test_record_fact_does_not_commit_an_invalid_fact():
    import add_fact_service
    git = FakeGit()
    deps, files = _record_deps(git)
    out = add_fact_service.record_fact(deps, _fact(statement="   "))
    assert not out.ok and out.refused is None and out.added.errors
    assert files.records == [] and git.commits == []


def test_record_fact_reports_a_failed_commit_but_keeps_the_fact():
    import add_fact_service
    git = FakeGit(commit_fails=True)
    deps, files = _record_deps(git)
    out = add_fact_service.record_fact(deps, _fact())
    assert out.added.ok and out.commit is None and out.commit_error == "index.lock exists"
    assert len(files.records) == 1


def test_library_and_composition_modules_import_without_a_private_repo():
    """Configuration is injected, not read at import: with no knowledge-private checkout at all, every
    composition root, the read adapter and the Typer app still import. Only the first use of a path fails."""
    import subprocess
    import glob
    steps = sorted(os.path.splitext(p)[0].replace("/", ".") for p in
                   glob.glob("ingest/*.py") + glob.glob("client/*.py") if not p.endswith("__init__.py"))
    code = ("import add_fact, review, lifecycle, facts_batch, migrate_memory, rules_edit, entities, subjects, "
            "fact_queries, knowledge, cli_add_fact, build, queries, " + ", ".join(steps) + "; print('ok')")
    env = {**os.environ, "KNOWLEDGE_PRIVATE_DIR": "/nonexistent/knowledge-private"}
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
    r = subprocess.run([sys.executable, "-c", "import container; container.Container().add_fact.data_file_path()"], cwd=REPO, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "local_paths.py not found" in r.stderr
