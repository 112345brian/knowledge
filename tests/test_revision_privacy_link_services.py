"""The revision, privacy-floor and entity-link use cases against fake ports (no sqlite, no file, no clock), the
SQLite adapters against a real in-memory schema, and the two structural guards: the ports carry no SQL, and
nobody discards the unprotected-mention result."""
import ast
import dataclasses
import inspect
import os
import sqlite3

import pytest

import entity_link_rules
import entity_link_service
import entity_repository_store
import fact_privacy_table_port
import fact_revision_table_port
import fact_state
import fact_tables_store
import build_records as r
from build_repositories_port import EntityRepository
from entity_rules import Entity
import privacy
import privacy_floor_service
import revision_apply_rules
import revision_apply_service
import revisions
from fact_privacy_table_port import FactText
from test_add_fact import REPO

T1, T2, T3 = "2026-10-01T08:00:00+00:00", "2026-10-02T09:00:00+00:00", "2026-10-03T10:00:00+00:00"


def rec(key, n, at, **ch):
    base = {"statement": "S", "trust_level": "low", "trust_rationale": None, "status": "active", "visibility": "normal",
            "superseded_by": None, "recheck_by": None, "recheck_rationale": "r", "freshness": "no-decay",
            "kind": "unclassified", "valid_from": None, "valid_to": None, "applies_to": None, "notes": None}
    base.update(ch)
    return {"source_key": key, "revision": n, "changed_at": at, "changed_via": "t", "session_id": None,
            "change_reason": "r", **base}


# ------------------------------------------------------------------ fakes (one shared call log)


class World:
    """In-memory facts and history behind the three table ports; `calls` records the order of every port call."""

    def __init__(self, facts=(), log=(), rules=None, subjects=None, texts=None):
        self.calls = []
        self.facts = {i: dict(f) for i, f in enumerate(facts, start=1)}      # id -> {source_key, visibility, subject, statement}
        self.history = [{"fact_id": i, "visibility": f["visibility"], "revision": 1} for i, f in self.facts.items()]
        self.state = {}
        self.log, self.rules = list(log), rules
        self.subjects = dict(subjects or {})
        self.subject_flags = {}
        self.committed = 0
        self.links, self.entity_rows, self.aliases = [], {}, []


class FakeLog:
    def __init__(self, w):
        self.w = w

    def read_log(self, path, base=None):
        self.w.calls.append(("read_log", path))
        return [(n, r) for n, r in enumerate(self.w.log, start=1)]


class FakeRules:
    def __init__(self, w):
        self.w = w

    def load_rules(self, path):
        self.w.calls.append(("load_rules", path))
        return self.w.rules


class FakeRevisionTable:
    def __init__(self, w):
        self.w = w

    def fact_ids_by_source_key(self):
        return {f["source_key"]: i for i, f in self.w.facts.items()}

    def original_revisions(self):
        return {f["source_key"]: {"changed_at": T1, "freshness": "no-decay", "kind": "unclassified", "valid_from": None,
                                  "valid_to": None, "applies_to": None} for f in self.w.facts.values()}

    def record_revision(self, fact_id, revision):
        self.w.calls.append(("record_revision", fact_id, revision["revision"]))
        self.w.history.append({"fact_id": fact_id, "visibility": revision["visibility"], "revision": revision["revision"]})

    def write_current_state(self, fact_id, state):
        self.w.calls.append(("write_current_state", fact_id))
        self.w.state[fact_id] = dict(state)
        self.w.facts[fact_id]["visibility"] = state["visibility"]
        self.w.facts[fact_id]["statement"] = state["statement"]

    def visibility_by_fact(self):
        return {i: f["visibility"] for i, f in self.w.facts.items()}

    def raise_history_to_private(self, fact_ids):
        self.w.calls.append(("raise_history", sorted(fact_ids)))
        for h in self.w.history:
            if h["fact_id"] in set(fact_ids):
                h["visibility"] = "private"

    def revision_count(self):
        return len(self.w.history)

    def commit(self):
        self.w.calls.append(("commit",))
        self.w.committed += 1


class FakePrivacyTable:
    def __init__(self, w):
        self.w = w

    def subject_parents(self):
        return dict(self.w.subjects)

    def mark_subject(self, name, private):
        self.w.calls.append(("mark_subject", name, private))
        self.w.subject_flags[name] = private

    def facts_to_scan(self):
        return [FactText(i, f["subject"], f["statement"], f["visibility"], tuple(f.get("extra", ())))
                for i, f in self.w.facts.items()]

    def raise_to_private(self, fact_id):
        self.w.calls.append(("raise_to_private", fact_id))
        self.w.facts[fact_id]["visibility"] = "private"


class FakeEntities:
    """An `EntityRepository` over the world: rows are numbered from 100, in call order."""

    def __init__(self, w):
        self.w = w

    def read_file(self, path):
        return None

    def add(self, entity):
        row = len(self.w.entity_rows) + 100
        name_norm, *alias_norms = entity.terms
        self.w.entity_rows[entity.id] = (row, name_norm)
        self.w.aliases.extend((row, a, n) for a, n in zip(entity.aliases, alias_norms))
        return r.EntityRowId(row)

    def fact_texts(self):
        return [r.FactText(r.FactId(i), (f["statement"], *f.get("extra", ()))) for i, f in self.w.facts.items()]

    def link_fact(self, fact_id, entity_row_id):
        self.w.links.append((fact_id, entity_row_id))

    def fact_visibility(self, fact_id):
        f = self.w.facts.get(fact_id)
        return None if f is None else f["visibility"]


def revision_deps(w):
    return revision_apply_service.RevisionApplyDeps(revision_log=FakeLog(w), revision_table=FakeRevisionTable(w),
                                                    privacy_table=FakePrivacyTable(w), rules_reader=FakeRules(w))


def fact(key, statement="Plain.", visibility="normal", subject="s", extra=()):
    return {"source_key": key, "statement": statement, "visibility": visibility, "subject": subject, "extra": extra}


# ------------------------------------------------------------------ revision use case


def test_the_pass_applies_revisions_then_rules_then_raises_history_then_commits_in_that_order():
    w = World(facts=[fact("a"), fact("b", visibility="private")],
              log=[rec("a", 2, T2, statement="Now about Quenby.")],
              rules=privacy.Rules(keywords=("quenby",)), subjects={"s": None})
    run = revision_apply_service.apply_fact_revisions(revision_deps(w), "/d/log.jsonl", "/d/rules.json")
    names = [c[0] for c in w.calls]
    assert names == ["read_log", "record_revision", "write_current_state", "commit", "load_rules", "mark_subject",
                     "raise_to_private", "raise_history", "commit"]
    assert w.calls[0] == ("read_log", "/d/log.jsonl") and w.calls[4] == ("load_rules", "/d/rules.json")
    assert (run.applied, [f for f, _ in run.raised], run.total_revisions) == (1, [1], 3)
    assert w.facts[1]["visibility"] == "private"
    assert all(h["visibility"] == "private" for h in w.history)       # a's snapshots and b's, both private facts


def test_an_invalid_log_stops_before_anything_is_written_or_any_rule_is_loaded():
    w = World(facts=[fact("a")], log=[rec("a", 3, T2)], rules=privacy.Rules(), subjects={"s": None})
    with pytest.raises(revisions.RevisionError, match="expected 2"):
        revision_apply_service.apply_fact_revisions(revision_deps(w), "/d/log.jsonl", "/d/rules.json")
    assert [c[0] for c in w.calls] == ["read_log"]
    assert w.committed == 0 and len(w.history) == 1


def test_an_empty_log_still_applies_the_floor_and_raises_history():
    w = World(facts=[fact("a", statement="Quenby again.")], log=[], rules=privacy.Rules(keywords=("quenby",)),
              subjects={"s": None})
    run = revision_apply_service.apply_fact_revisions(revision_deps(w), "/d/none", "/d/rules.json")
    assert run.applied == 0 and [f for f, _ in run.raised] == [1]
    assert w.history == [{"fact_id": 1, "visibility": "private", "revision": 1}]


def test_only_the_latest_revision_of_a_fact_is_written_back_and_a_replacement_key_becomes_its_fact_id():
    w = World(facts=[fact("a"), fact("b")], log=[
        rec("a", 2, T2, statement="A2"), rec("b", 2, T2, status="superseded", superseded_by="a"),
        rec("a", 3, T3, statement="A3", kind="plan")], rules=privacy.Rules(), subjects={"s": None})
    revision_apply_service.apply_revision_log(revision_apply_service.RevisionLogDeps(FakeLog(w), FakeRevisionTable(w)), "p")
    assert [c for c in w.calls if c[0] == "record_revision"] == [("record_revision", 1, 2), ("record_revision", 2, 2),
                                                                  ("record_revision", 1, 3)]
    assert w.state[1]["statement"] == "A3" and w.state[1]["kind"] == "plan"
    assert w.state[2]["superseded_by_fact_id"] == 1 and set(w.state[2]) == set(revision_apply_rules.STATE_FIELDS)


def test_state_fields_cover_every_mutable_field():
    assert set(revision_apply_rules.STATE_FIELDS) == {"superseded_by_fact_id" if f == "superseded_by" else f
                                                     for f in revisions.MUTABLE_FIELDS}


def test_history_is_raised_only_for_private_facts():
    assert fact_state.facts_with_private_history({3: "normal", 1: "private", 2: "private"}) == [1, 2]
    assert fact_state.facts_with_private_history({}) == []


# ------------------------------------------------------------------ privacy floor use case


def test_floor_marks_subjects_by_own_or_ancestor_tag_and_fails_closed_on_a_cycle():
    w = World(subjects={"root": None, "child": "root", "free": None, "x": "y", "y": "x"})
    out = privacy_floor_service.apply_privacy_floor(privacy_floor_service.PrivacyFloorDeps(FakePrivacyTable(w)),
                                                    privacy.Rules(subject_tags={"root": "private"}))
    assert w.subject_flags == {"root": True, "child": True, "free": False, "x": True, "y": True}
    assert out == {"raised": [], "private_subjects": 4}


def test_floor_raises_by_subject_keyword_and_other_text_and_never_lowers():
    w = World(subjects={"secret": None, "s": None}, facts=[
        fact("a", subject="secret"), fact("b", statement="Quenby."), fact("c", extra=("a note on quenby",)),
        fact("d", visibility="private"), fact("e")])
    out = privacy_floor_service.apply_privacy_floor(privacy_floor_service.PrivacyFloorDeps(FakePrivacyTable(w)),
                                                    privacy.Rules(subject_tags={"secret": "private"}, keywords=("quenby",)))
    assert [f for f, _ in out["raised"]] == [1, 2, 3]
    assert [f["visibility"] for f in w.facts.values()] == ["private"] * 4 + ["normal"]
    assert "subject-tag" in out["raised"][0][1] and "keyword" in out["raised"][1][1]


def test_floor_uses_the_private_entities_carried_by_the_rules():
    w = World(subjects={"s": None}, facts=[fact("a", statement="Lunch with Zeddy.")])
    rules = privacy.Rules(entities=(("Zed Name", ("zed name", "zeddy")),))
    out = privacy_floor_service.apply_privacy_floor(privacy_floor_service.PrivacyFloorDeps(FakePrivacyTable(w)), rules)
    assert [f for f, _ in out["raised"]] == [1] and "entity" in out["raised"][0][1]


# ------------------------------------------------------------------ entity link use case


def ent(id, name, private=False, aliases=()):
    return Entity(id, name, "person", tuple(aliases), private)


def test_link_writes_entities_and_aliases_before_any_link_and_normalizes_names():
    w = World(facts=[fact("a", statement="With Zeddy.", visibility="private"), fact("b", extra=("zed name",), visibility="private")])
    out = entity_link_service.link_entities(entity_link_service.EntityLinkDeps(FakeEntities(w)),
                                            [ent("zed", "Zed  Name", private=True, aliases=["Zeddy"])])
    assert out.as_dict() == {"entities": 1, "links": 2, "unprotected": []}
    assert w.entity_rows["zed"][1] == "zed name" and w.aliases == [(100, "Zeddy", "zeddy")]
    assert w.links == [(1, 100), (2, 100)]


def test_a_private_mention_stored_normal_is_listed_by_link_facts_and_refused_by_link_entities():
    w = World(facts=[fact("a", statement="About Zed.", visibility="normal"), fact("b", statement="About Acme.")])
    deps = entity_link_service.EntityLinkDeps(FakeEntities(w))
    es = [ent("zed", "Zed", private=True), ent("acme", "Acme")]
    assert entity_link_service.link_facts(deps, es).unprotected == [1]
    with pytest.raises(entity_link_rules.UnprotectedPrivateMention, match=r"facts \[1\] mention") as e:
        entity_link_service.link_entities(deps, es)
    assert isinstance(e.value, ValueError) and "refusing to build" in str(e.value)


def test_the_refusal_names_at_most_ten_facts_and_a_public_mention_of_a_public_entity_is_fine():
    w = World(facts=[fact(f"k{i}", statement="Zed.", visibility="normal") for i in range(12)])
    with pytest.raises(entity_link_rules.UnprotectedPrivateMention) as e:
        entity_link_service.link_entities(entity_link_service.EntityLinkDeps(FakeEntities(w)), [ent("zed", "Zed", private=True)])
    assert e.value.fact_ids == list(range(1, 13)) and str(e.value).startswith("facts [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] mention")
    w = World(facts=[fact("a", statement="Acme.")])
    assert entity_link_service.link_entities(entity_link_service.EntityLinkDeps(FakeEntities(w)), [ent("acme", "Acme")]).links == 1


def test_blank_and_missing_text_never_matches():
    w = World(facts=[fact("a", statement="", extra=(None, "", "  "))])
    out = entity_link_service.link_entities(entity_link_service.EntityLinkDeps(FakeEntities(w)), [ent("x", "Xylo")])
    assert out.links == 0


# ------------------------------------------------------------------ SQLite adapters


def new_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    return con


def seed(con):
    con.execute("INSERT INTO subjects (id, name) VALUES (1, 'root'), (3, 'other')")
    con.execute("INSERT INTO subjects (id, name, parent_id) VALUES (2, 'child', 1)")
    for i, (key, subj, stmt, vis) in enumerate([("a", 2, "About Zed.", "normal"), ("b", 3, "Plain.", "private")], start=1):
        con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, recheck_rationale, visibility, "
                    "source_key, notes) VALUES (?, ?, ?, 'low', 'no-decay', 'r', ?, ?, 'a note')", (i, subj, stmt, vis, key))
        r1 = revisions.implicit_revision(key, {"statement": stmt, "trust_level": "low", "freshness": "no-decay",
                                               "visibility": vis, "recheck_rationale": "r", "subject": "x",
                                               "kind": "plan", "valid_from": "2020"}, T1)
        fact_tables_store.insert_revision(con.cursor(), i, r1)


def test_sqlite_revision_table_round_trip():
    con = new_db()
    seed(con)
    t = fact_tables_store.SqliteFactRevisionTable(con)
    assert t.fact_ids_by_source_key() == {"a": 1, "b": 2}
    orig = t.original_revisions()
    assert orig["a"] == {"changed_at": T1, "freshness": "no-decay", "kind": "plan", "valid_from": "2020",
                         "valid_to": None, "applies_to": None}
    t.record_revision(1, rec("a", 2, T2, statement="New", visibility="private", status="superseded", superseded_by="b"))
    state = revision_apply_rules.current_state(rec("a", 2, T2, statement="New", visibility="private",
                                                   status="superseded", superseded_by="b", applies_to="men"), {"a": 1, "b": 2})
    t.write_current_state(1, state)
    row = con.execute("SELECT statement, visibility, status, superseded_by_fact_id, applies_to FROM facts WHERE id = 1").fetchone()
    assert tuple(row) == ("New", "private", "superseded", 2, "men")
    assert t.revision_count() == 3 and t.visibility_by_fact() == {1: "private", 2: "private"}
    t.raise_history_to_private([1, 2])
    assert {r[0] for r in con.execute("SELECT visibility FROM fact_revisions")} == {"private"}
    t.raise_history_to_private([])           # nothing to do, no error
    assert con.in_transaction is True
    t.commit()
    assert con.in_transaction is False


def test_sqlite_raise_history_handles_more_ids_than_one_chunk():
    con = new_db()
    seed(con)
    t = fact_tables_store.SqliteFactRevisionTable(con)
    t.raise_history_to_private(list(range(1, 2000)))
    assert {r[0] for r in con.execute("SELECT visibility FROM fact_revisions")} == {"private"}


def test_sqlite_privacy_table_reads_parents_texts_and_writes_flags_and_raises():
    con = new_db()
    seed(con)
    con.execute("INSERT INTO sources (id, citekey, name, source_type) VALUES (1, 'c', 'n', 'primary')")
    con.execute("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (1, 1, 'p. 3', 'said Zed')")
    t = fact_tables_store.SqliteFactPrivacyTable(con)
    assert t.subject_parents() == {"root": None, "child": "root", "other": None}
    scanned = {f.fact_id: f for f in t.facts_to_scan()}
    assert scanned[1].subject == "child" and scanned[1].visibility == "normal"
    assert scanned[1].extra_text[0] == "a note" and "said Zed" in scanned[1].extra_text[-1]
    assert scanned[2].extra_text[-1] is None             # no citations: the concatenation is NULL
    t.mark_subject("root", True)
    t.raise_to_private(1)
    assert con.execute("SELECT private FROM subjects WHERE name = 'root'").fetchone()[0] == 1
    assert con.execute("SELECT visibility FROM facts WHERE id = 1").fetchone()[0] == "private"


def test_sqlite_entity_repository_writes_rows_and_reads_texts_in_id_order():
    con = new_db()
    seed(con)
    t = entity_repository_store.SqliteEntityRepository(con)
    row = t.add(Entity("zed", "Zed  Name", "person", ("Zeddy",), True))
    texts = t.fact_texts()
    assert [m.fact_id for m in texts] == [1, 2] and t.fact_visibility(r.FactId(1)) == "normal"
    assert texts[0].texts[0] == "About Zed." and "a note" in texts[0].texts
    assert t.fact_visibility(r.FactId(99)) is None
    t.link_fact(r.FactId(1), row)
    assert con.execute("SELECT private, name_norm FROM entities").fetchone()[:] == (1, "zed name")
    assert con.execute("SELECT alias, alias_norm FROM entity_aliases").fetchall()[0][:] == ("Zeddy", "zeddy")
    assert con.execute("SELECT fact_id, entity_id FROM fact_entities").fetchall()[0][:] == (1, row)
    with pytest.raises(sqlite3.IntegrityError):          # the schema's trigger still guards alias/name collisions
        t.add(Entity("other", "Other", "person", ("zed name",)))


def test_the_repository_adapter_decides_nothing_and_never_commits():
    """A mention of a private entity in a normal fact is stored and reported by the service, not refused here."""
    con = new_db()
    seed(con)
    deps = entity_link_service.EntityLinkDeps(entity_repository_store.SqliteEntityRepository(con))
    out = entity_link_service.link_facts(deps, [ent("zed", "Zed", private=True)])
    assert out.as_dict() == {"entities": 1, "links": 1, "unprotected": [1]} and con.in_transaction is True


# ------------------------------------------------------------------ structural guards


def _port_protocols():
    for mod in (fact_revision_table_port, fact_privacy_table_port):
        for name, cls in vars(mod).items():
            if inspect.isclass(cls) and getattr(cls, "_is_protocol", False) and cls.__module__ == mod.__name__:
                yield cls
    yield EntityRepository          # the entity-link use case writes through the foundation's port


def test_the_ports_have_semantic_methods_not_sql_ones():
    banned = ("sql", "query", "execute", "select", "cursor", "fetch", "row", "connection")
    methods = [(c.__name__, m) for c in _port_protocols() for m in vars(c) if not m.startswith("_")]
    assert len(methods) >= 15
    assert [(c, m) for c, m in methods if any(b in m for b in banned)] == []
    for c in _port_protocols():
        for m, fn in vars(c).items():
            if callable(fn) and not m.startswith("_"):
                assert "con" not in inspect.signature(fn).parameters, f"{c.__name__}.{m} takes a connection"


def test_every_fake_and_adapter_has_exactly_the_ports_methods():
    pairs = [(fact_revision_table_port.FactRevisionTable, FakeRevisionTable, fact_tables_store.SqliteFactRevisionTable),
             (fact_privacy_table_port.FactPrivacyTable, FakePrivacyTable, fact_tables_store.SqliteFactPrivacyTable),
             (EntityRepository, FakeEntities, entity_repository_store.SqliteEntityRepository)]
    for port, fake, real in pairs:
        wanted = {m for m in vars(port) if not m.startswith("_")}
        for impl in (fake, real):
            have = {m for m in vars(impl) if not m.startswith("_")}
            assert have == wanted, (impl.__name__, have ^ wanted)
            for m in wanted:
                assert list(inspect.signature(getattr(impl, m)).parameters) == list(inspect.signature(getattr(port, m)).parameters)


def test_no_caller_discards_the_unprotected_mention_result():
    """`link_facts` only reports the facts that mention a private entity without being private. Any other module
    that calls it (or the store/step wrappers around it) must also look at `unprotected` or call
    `link_entities`, which refuses; a caller that ignores the list would let the build ship the leak."""
    wrappers = {"link_facts", "link_entities"}
    homes = {"entity_link_service.py"}
    offenders, inspected = [], []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".venv", ".git", "tests", "node_modules", ".claude"}]
        for f in files:
            if not f.endswith(".py") or f in homes:
                continue
            tree = ast.parse(open(os.path.join(root, f)).read())
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
                         for n in ast.walk(fn) if isinstance(n, ast.Call)}
                if calls & wrappers:
                    inspected.append(f"{f}:{fn.name}")
                    src = ast.unparse(fn)
                    if "link_entities(" not in src.replace("link_facts(", "") and "unprotected" not in src:
                        offenders.append(f"{os.path.relpath(os.path.join(root, f), REPO)}:{fn.name}")
    assert offenders == []
    assert "link_entities.py:run" in inspected          # the scan sees the build step, so it cannot pass vacuously


def test_only_the_entity_repository_adapter_writes_the_entity_tables():
    """Entity rows, alias rows and fact links are written by `entity_repository_store` (the build's EntityRepository)
    and by nothing else; `leak_test` seeds a throwaway database for the leak scan and is the one other writer."""
    import re
    allowed = {"entity_repository_store.py", "leak_test.py"}
    writer = re.compile(r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO)\s+(entities|entity_aliases|fact_entities)\b", re.I)
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".venv", ".git", "tests", "node_modules", ".claude"}]
        for f in files:
            if f.endswith(".py") and f not in allowed:
                if writer.search(open(os.path.join(root, f), encoding="utf-8").read()):
                    offenders.append(os.path.relpath(os.path.join(root, f), REPO))
    assert offenders == []
    assert writer.search(open(os.path.join(REPO, "entity_repository_store.py"), encoding="utf-8").read())
