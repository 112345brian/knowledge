"""Hexagonal architecture: the ports (ports.py) and the use cases that depend on them.

* every adapter module really satisfies the protocol its facade hands to a use case;
* a use case runs against in-memory fakes, with no file, db, git or clock (the point of a port);
* a use case touches `os` only for os.path string handling.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ports  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = ("review_service", "lifecycle_service", "add_fact_service", "facts_batch_service", "migrate_memory_service")


def test_every_facade_binds_adapters_that_satisfy_the_protocols():
    import add_fact, add_fact_store, clock, ids, lifecycle, lifecycle_store, migrate_memory, migrate_memory_store
    import facts_batch, private_git, privacy_store, review, review_store, revisions_store
    for facade in (review, lifecycle, add_fact, facts_batch, migrate_memory):
        bundle = facade.PORTS
        for name in ports.PORT_PROTOCOLS:
            adapter = getattr(bundle, name)
            if adapter is None:
                continue
            assert ports.conformance_problems(name, adapter) == [], f"{facade.__name__}.PORTS.{name}"
    # the adapters themselves, so a rename fails here even if no facade binds that port
    for name, adapter in {"git": private_git, "revisions": revisions_store, "rules": privacy_store,
                          "reads": review_store, "lifecycle_reads": lifecycle_store, "facts_file": add_fact_store,
                          "clock": clock, "ids": ids, "memory": migrate_memory_store}.items():
        assert ports.conformance_problems(name, adapter) == [], name


def test_conformance_check_can_fail():
    class Bad:
        def find_repo(self):  # wrong parameters, and the rest is missing
            return None
    problems = ports.conformance_problems("git", Bad())
    assert any("find_repo" in p for p in problems) and any("ensure_clean_tree is missing" in p for p in problems)


def test_bind_keeps_the_public_signature_without_ports():
    import inspect

    def fn(ports_, a, b=2):
        """doc"""
        return (ports_, a, b)
    bound = ports.bind(fn, "P")
    assert bound(1) == ("P", 1, 2) and bound.__doc__ == "doc"
    assert list(inspect.signature(bound).parameters) == ["a", "b"]


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
    p = ports.Ports(reads=reads, revisions=fake_rev)
    result = review_service.approve(p, ["k1", "k2", "nope"], reason="ok", commit=False)
    assert [(i.ref, i.outcome) for i in result.items] == [("k1", "approved"), ("k2", "skipped"), ("nope", "unknown")]
    assert fake_rev.calls == [("k1", {"status": "active"}, "ok", {"status": "pending"})]


def test_review_service_reports_a_failed_write_per_item():
    import review_service
    import revisions

    class Failing(FakeRevisions):
        def append_revision(self, key, *a, **k):
            return revisions.RevisionResult(False, ["disk on fire"])
    p = ports.Ports(reads=FakeReads({"k1": {"status": "pending"}}), revisions=Failing())
    (item,) = review_service.approve(p, ["k1"], commit=False).items
    assert item.outcome == "error" and "disk on fire" in item.reason


def _service_trees():
    for name in SERVICES + ("ports",):
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
