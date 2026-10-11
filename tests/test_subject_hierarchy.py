"""The subject hierarchy lane: the pure decisions (`subject_rules`), the application service against an in-memory
repository, and the SQLite adapter against the real schema. The step itself is pinned by tests/test_subjects.py."""
import os
import sqlite3

import pytest

import subject_hierarchy_service as svc
import subject_repository_store
import subject_rules
from subject_rules import AliasRefused, HierarchyTally

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def entry(name, **kw):
    return {"name": name, "domain": None, "parent": None, "relation": None, "description": None, "aliases": [],
            "deprecated": False, "replaced_by": None, **kw}


class FakeSubjects:
    """The repository port over dicts, with the same rules the schema's triggers enforce, plus a call log."""

    def __init__(self, existing=()):
        self.rows, self.aliases, self.log = {}, {}, []
        self._next = 1
        for name, domain in existing:
            self.create_subject(name, domain)
        self.log.clear()

    def find_subject(self, name):
        return self.rows[name]["id"] if name in self.rows else None

    def find_alias(self, alias):
        return self.rows[self.aliases[alias]]["id"] if alias in self.aliases else None

    def subject_ids(self):
        return {n: r["id"] for n, r in self.rows.items()}

    def alias_owners(self):
        return dict(self.aliases)

    def create_subject(self, name, domain=None):
        self.log.append(("create", name, domain))
        self.rows[name] = {"id": self._next, "domain": domain or "health-and-fitness", "parent": None,
                           "relation": "broader", "description": None, "deprecated": False, "replaced_by": None}
        self._next += 1
        return self.rows[name]["id"]

    def ensure_subject(self, name, domain):
        if name not in self.rows:
            self.create_subject(name, domain)

    def _by_id(self, i):
        return next(r for r in self.rows.values() if r["id"] == i)

    def place(self, subject_id, parent_id, relation, description, deprecated):
        self.log.append(("place", subject_id, parent_id, relation))
        self._by_id(subject_id).update(parent=parent_id, relation=relation, description=description, deprecated=deprecated)

    def replace_with(self, subject_id, replacement_id):
        self._by_id(subject_id)["replaced_by"] = replacement_id

    def add_alias(self, subject_id, alias):
        if alias in self.rows or alias in self.aliases:
            raise AliasRefused("alias collides with a subject name")
        self.aliases[alias] = next(n for n, r in self.rows.items() if r["id"] == subject_id)

    def tally(self):
        return HierarchyTally(sum(1 for r in self.rows.values() if r["parent"] is not None), len(self.aliases),
                              sum(1 for r in self.rows.values() if r["deprecated"]))


def deps(repo):
    return svc.HierarchyDeps(subjects=repo)


# ------------------------------------------------------------------ pure decisions

def test_plan_defaults_domain_and_relation_and_keeps_file_order():
    plans = subject_rules.plan_hierarchy([
        entry("b", parent="a", relation="part-of", domain="music"), entry("a"), entry("c", parent="a")])
    assert [p.name for p in plans] == ["b", "a", "c"]
    assert (plans[0].domain, plans[0].relation) == ("music", "part-of")
    assert (plans[1].domain, plans[1].relation) == (subject_rules.DEFAULT_DOMAIN, subject_rules.DEFAULT_RELATION)
    assert plans[2].relation == "broader"


def test_a_relation_without_a_parent_is_the_default_relation():
    assert subject_rules.plan_hierarchy([entry("a", relation="part-of")])[0].relation == subject_rules.DEFAULT_RELATION


def test_identify_name_beats_alias_and_unknown_has_no_key():
    names, aliases = {"x": 1}, {"x": 2, "y": 3}
    assert subject_rules.identify("x", names.get, aliases.get) == (1, "name")
    assert subject_rules.identify("y", names.get, aliases.get) == (3, "alias")
    assert subject_rules.identify("z", names.get, aliases.get) == (None, "unknown")


def test_canonical_over_file_entries_is_the_same_identity():
    entries = [entry("steroids", aliases=["aas"])]
    assert subject_rules.canonical("steroids", entries) == ("steroids", "name")
    assert subject_rules.canonical("aas", entries) == ("steroids", "alias")
    assert subject_rules.canonical("other", entries) == ("other", "unknown")
    assert subject_rules.canonical("aas", None) == ("aas", "unknown")


def test_refuse_alias_names_the_cause():
    assert subject_rules.refuse_alias("a", {"a": 1}, {}) == "alias collides with a subject name"
    assert "belongs to 's'" in subject_rules.refuse_alias("a", {}, {"a": "s"})
    assert subject_rules.refuse_alias("a", {"s": 1}, {"b": "s"}) is None


# ------------------------------------------------------------------ the service on a fake repository

def test_load_creates_places_replaces_and_claims():
    repo = FakeSubjects(existing=[("topic-a", "example"), ("topic-a-detail", None)])
    t = svc.load_hierarchy(deps(repo), [
        entry("topic-a", domain="other", aliases=["ta"]),
        entry("topic-a-detail", parent="topic-a", relation="part-of", description="sub"),
        entry("old", deprecated=True, replaced_by="new"),   # replacement listed after the subject
        entry("new", domain="music")])
    assert repo.rows["topic-a"]["domain"] == "example"       # a stored subject keeps its domain
    assert repo.rows["new"]["domain"] == "music"
    assert repo.rows["topic-a-detail"]["parent"] == repo.rows["topic-a"]["id"]
    assert repo.rows["topic-a-detail"]["relation"] == "part-of"
    assert repo.rows["old"]["replaced_by"] == repo.rows["new"]["id"]
    assert repo.aliases == {"ta": "topic-a"}
    assert t == HierarchyTally(with_parent=1, aliases=1, deprecated=1)


def test_every_subject_is_created_before_any_is_placed():
    repo = FakeSubjects()
    svc.load_hierarchy(deps(repo), [entry("child", parent="parent"), entry("parent")])
    kinds = [c[0] for c in repo.log]
    assert kinds == ["create", "create", "place", "place"]


def test_alias_equal_to_a_stored_subject_name_is_refused_naming_both():
    repo = FakeSubjects(existing=[("already-a-subject", None)])
    with pytest.raises(ValueError, match=r"alias 'already-a-subject' of 'main' cannot be loaded \(alias collides with a subject name\)"):
        svc.load_hierarchy(deps(repo), [entry("main", aliases=["already-a-subject"])])


def test_alias_claimed_by_another_subject_is_refused():
    repo = FakeSubjects()
    repo.create_subject("one")
    repo.add_alias(repo.find_subject("one"), "shared")
    with pytest.raises(ValueError, match="alias 'shared' of 'two' cannot be loaded"):
        svc.load_hierarchy(deps(repo), [entry("two", aliases=["shared"])])


def test_a_refusal_from_the_adapter_is_reported_the_same_way():
    class Strict(FakeSubjects):
        def add_alias(self, subject_id, alias):
            raise AliasRefused("UNIQUE constraint failed: subject_aliases.alias")
    with pytest.raises(ValueError, match=r"alias 'x' of 'a' cannot be loaded \(UNIQUE constraint failed"):
        svc.load_hierarchy(deps(Strict()), [entry("a", aliases=["x"])])


def test_loading_nothing_changes_nothing():
    repo = FakeSubjects(existing=[("a", None)])
    assert svc.load_hierarchy(deps(repo), []) == HierarchyTally(0, 0, 0)
    assert repo.log == []


# ------------------------------------------------------------------ the subject a fact is filed under

def test_subject_for_fact_name_then_alias_then_create_with_a_cache():
    repo = FakeSubjects(existing=[("steroids", "health")])
    repo.add_alias(repo.find_subject("steroids"), "aas")
    cache = {}
    sid = repo.find_subject("steroids")
    assert svc.subject_for_fact(deps(repo), "steroids", None, cache) == sid
    assert svc.subject_for_fact(deps(repo), "aas", None, cache) == sid       # filed under the canonical subject
    assert "aas" not in repo.rows
    new = svc.subject_for_fact(deps(repo), "fresh", "general", cache)
    assert repo.rows["fresh"]["domain"] == "general" and new == repo.rows["fresh"]["id"]
    repo.log.clear()
    assert svc.subject_for_fact(deps(repo), "fresh", "general", cache) == new
    assert repo.log == []                                                      # the cache answered


def test_subject_for_fact_without_a_domain_leaves_the_default_to_the_store():
    repo = FakeSubjects()
    svc.subject_for_fact(deps(repo), "x")
    assert repo.log == [("create", "x", None)]


# ------------------------------------------------------------------ the SQLite adapter on the real schema

@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(REPO, "schema.sql")) as f:
        c.executescript(f.read())
    return c


def test_adapter_runs_the_same_load_as_the_fake(con):
    repo = subject_repository_store.SqliteSubjects(con)
    con.execute("INSERT INTO subjects (name, domain) VALUES ('topic-a', 'example')")
    t = svc.load_hierarchy(deps(repo), [
        entry("topic-a", domain="other", aliases=["ta"]),
        entry("detail", parent="topic-a", relation="part-of", description="sub"),
        entry("old", deprecated=True, replaced_by="topic-a")])
    assert t == HierarchyTally(1, 1, 1)
    r = {x[0]: x for x in con.execute("SELECT name, domain, parent_relation, description, deprecated FROM subjects")}
    assert r["topic-a"][1] == "example" and r["detail"][2:4] == ("part-of", "sub") and r["old"][4] == 1
    assert con.execute("SELECT s.name FROM subjects s JOIN subjects o ON o.replaced_by_subject_id = s.id "
                       "WHERE o.name = 'old'").fetchone()[0] == "topic-a"
    assert repo.alias_owners() == {"ta": "topic-a"}


def test_adapter_lookups_and_creation(con):
    repo = subject_repository_store.SqliteSubjects(con)
    assert repo.find_subject("x") is None and repo.find_alias("y") is None
    sid = repo.create_subject("x")
    assert repo.find_subject("x") == sid
    assert con.execute("SELECT domain FROM subjects WHERE id = ?", (sid,)).fetchone()[0] == subject_rules.DEFAULT_DOMAIN
    assert repo.create_subject("z", "general") != sid
    repo.add_alias(sid, "y")
    assert repo.find_alias("y") == sid and repo.subject_ids() == {"x": sid, "z": sid + 1}


def test_adapter_translates_trigger_and_unique_failures_to_alias_refused(con):
    repo = subject_repository_store.SqliteSubjects(con)
    a = repo.create_subject("a")
    repo.add_alias(a, "ay")
    with pytest.raises(AliasRefused, match="collides"):
        repo.add_alias(a, "a")
    with pytest.raises(AliasRefused, match="UNIQUE|constraint"):
        repo.add_alias(a, "ay")


def test_adapter_end_to_end_alias_refusal_message_matches_the_old_step(con):
    repo = subject_repository_store.SqliteSubjects(con)
    con.execute("INSERT INTO subjects (name) VALUES ('already-a-subject')")
    with pytest.raises(ValueError, match="already-a-subject") as e:
        svc.load_hierarchy(deps(repo), [entry("main", aliases=["already-a-subject"])])
    assert "it equals an existing subject name or another alias" in str(e.value)


def test_adapter_ensure_subject_keeps_the_stored_domain(con):
    repo = subject_repository_store.SqliteSubjects(con)
    con.execute("INSERT INTO subjects (name, domain) VALUES ('x', 'general')")
    repo.ensure_subject("x", "music")
    assert con.execute("SELECT domain FROM subjects WHERE name = 'x'").fetchone()[0] == "general"
