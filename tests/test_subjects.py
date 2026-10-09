"""#43: the subject hierarchy as data (subjects.json), aliases, deprecation, relation types.

Pins the pre-#43 hierarchy first (the built-in table and the exported file must give the same subject
rows), then covers the library rules, step 06, alias resolution in the writers and queries, the
`subject` CLI group with its git flow, the exporter and the normal-only DB. Temp dirs only.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest

import subjects
from test_add_fact import REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _restore_modules():
    before = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


def load06():
    spec = importlib.util.spec_from_file_location("seed06", os.path.join(REPO, "06_seed_subject_hierarchy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def new_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    return con


def tree(con):
    """{name: (parent name, domain)}, the pre-#43 notion of the hierarchy."""
    return {r["name"]: (r["parent"], r["domain"]) for r in con.execute(
        "SELECT s.name, s.domain, p.name AS parent FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id")}


def seed_ingested(con, extra=()):
    """The subjects 04 would have created for the real data: the two parents and all 24 children (domain default)."""
    mod = load06()
    for n in ["anabolic-steroids", *mod.AAS_CHILDREN, *mod.TRAINING_CHILDREN, *extra]:
        con.execute("INSERT INTO subjects (name) VALUES (?)", (n,))
    return mod


def entry(name, **kw):
    return {"name": name, **kw}


# ------------------------------------------------------------------ pin: the pre-#43 hierarchy

def test_builtin_hierarchy_is_the_documented_24_children_under_two_parents():
    con = new_db()
    mod = seed_ingested(con)
    mod.run_builtin(con)
    t = tree(con)
    assert len(t) == 1 + 1 + 14 + 10                       # anabolic-steroids, training, 14 aas-*, 10 training-*
    assert sum(1 for p, _ in t.values() if p) == 24
    assert t["training"] == (None, "health-and-fitness") and t["anabolic-steroids"] == (None, "health-and-fitness")
    assert {n for n, (p, _) in t.items() if p == "anabolic-steroids"} == set(mod.AAS_CHILDREN)
    assert {n for n, (p, _) in t.items() if p == "training"} == set(mod.TRAINING_CHILDREN)
    assert {r[0] for r in con.execute("SELECT parent_relation FROM subjects")} == {"broader"}


def test_builtin_run_leaves_absent_children_absent_and_only_creates_training():
    con = new_db()
    con.execute("INSERT INTO subjects (name) VALUES ('anabolic-steroids')")
    con.execute("INSERT INTO subjects (name) VALUES ('aas-legal')")
    load06().run_builtin(con)
    assert set(tree(con)) == {"anabolic-steroids", "aas-legal", "training"}


def test_exported_file_and_builtin_table_give_identical_subject_rows(tmp_path):
    mod = load06()
    a, b = new_db(), new_db()
    seed_ingested(a)
    seed_ingested(b)
    mod.run_builtin(a)
    mod.run_entries(b, subjects.parse(subjects.to_json(mod.builtin_entries())))
    assert tree(a) == tree(b)
    cols = "name, domain, parent_relation, deprecated, description, replaced_by_subject_id"
    assert sorted(map(tuple, a.execute(f"SELECT {cols} FROM subjects"))) == sorted(map(tuple, b.execute(f"SELECT {cols} FROM subjects")))


# ------------------------------------------------------------------ the library rules

def test_to_json_parse_round_trip_and_omits_defaults():
    entries = subjects.parse({"subjects": [
        entry("a"), entry("b", parent="a", relation="part-of", description="  note  ", aliases=["bee", "b2"]),
        entry("c", deprecated=True, replaced_by="a")]})
    data = subjects.to_json(entries)
    assert data["version"] == 1 and [e["name"] for e in data["subjects"]] == ["a", "b", "c"]
    assert data["subjects"][0] == {"name": "a"}
    assert data["subjects"][1] == {"name": "b", "parent": "a", "relation": "part-of", "description": "note", "aliases": ["b2", "bee"]}
    assert subjects.parse(data) == subjects.parse(json.loads(json.dumps(data)))


@pytest.mark.parametrize("bad, msg", [
    ([], "expected an object"),
    ({"subjects": "x"}, "'subjects' list"),
    ({"version": 2, "subjects": []}, "unsupported version"),
    ({"subjects": [5]}, "not an object"),
    ({"subjects": [{"name": "Bad Name"}]}, "kebab-case"),
    ({"subjects": [{}]}, "kebab-case"),
    ({"subjects": [entry("a"), entry("a")]}, "listed twice"),
    ({"subjects": [entry("a", colour="red")]}, "unknown key"),
    ({"subjects": [entry("a", domain=" ")]}, "domain"),
    ({"subjects": [entry("a", description=3)]}, "description"),
    ({"subjects": [entry("a", aliases="x")]}, "aliases"),
    ({"subjects": [entry("a", aliases=["Not Slug"])]}, "aliases"),
    ({"subjects": [entry("a", aliases=["x", "x"])]}, "same alias twice"),
    ({"subjects": [entry("a", deprecated="yes")]}, "deprecated"),
    ({"subjects": [entry("a", relation="broader")]}, "without a parent"),
    ({"subjects": [entry("a"), entry("b", parent="a", relation="kind-of")]}, "relation"),
    ({"subjects": [entry("a", parent="ghost")]}, "not a subject in the file"),
    ({"subjects": [entry("a", parent="a")]}, "itself as parent"),
    ({"subjects": [entry("a", parent="b"), entry("b", parent="a")]}, "parent cycle"),
    ({"subjects": [entry("a", replaced_by="b")]}, "not deprecated"),
    ({"subjects": [entry("a", deprecated=True, replaced_by="a")]}, "itself as replaced_by"),
    ({"subjects": [entry("a", deprecated=True, replaced_by="ghost")]}, "not a subject in the file"),
    ({"subjects": [entry("a", deprecated=True, replaced_by="b"), entry("b", deprecated=True, replaced_by="a")]}, "replaced_by cycle"),
    ({"subjects": [entry("a"), entry("b", aliases=["a"])]}, "claimed twice"),
    ({"subjects": [entry("a", aliases=["x"]), entry("b", aliases=["x"])]}, "claimed twice"),
    ({"subjects": [entry("a", aliases=["x"]), entry("b", parent="x")]}, "it is an alias"),
])
def test_invalid_files_are_refused_naming_the_problem(bad, msg):
    with pytest.raises(subjects.SubjectsError, match=msg):
        subjects.parse(bad)


def test_every_relation_type_is_accepted():
    for rel in subjects.RELATIONS:
        e = subjects.parse({"subjects": [entry("a"), entry("b", parent="a", relation=rel)]})
        assert e[1]["relation"] == rel
    assert subjects.RELATIONS == ("broader", "part-of", "subtype-of", "member-of")


def test_read_file_absent_corrupt_and_unreadable(tmp_path):
    assert subjects.read_file(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "s.json"
    bad.write_text("{not json")
    with pytest.raises(subjects.SubjectsError, match="not valid JSON"):
        subjects.read_file(str(bad))
    bad.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(subjects.SubjectsError):
        subjects.read_file(str(bad))


def test_canonical_and_check_new_fact():
    entries = subjects.parse({"subjects": [
        entry("steroids", aliases=["aas", "juice"]), entry("old-topic", deprecated=True, replaced_by="steroids", aliases=["ancient"]),
        entry("dead-end", deprecated=True)]})
    assert subjects.canonical("steroids", entries) == ("steroids", "name")
    assert subjects.canonical("aas", entries) == ("steroids", "alias")
    assert subjects.canonical("brand-new", entries) == ("brand-new", "unknown")
    assert subjects.canonical("aas", []) == ("aas", "unknown") and subjects.canonical("aas", None) == ("aas", "unknown")
    canon, notes, err = subjects.check_new_fact("juice", entries)
    assert (canon, err) == ("steroids", None) and "alias of 'steroids'" in notes[0]
    assert subjects.check_new_fact("steroids", entries) == ("steroids", [], None)
    canon, _, err = subjects.check_new_fact("old-topic", entries)
    assert canon == "old-topic" and "deprecated" in err and "'steroids'" in err
    assert "'steroids'" in subjects.check_new_fact("ancient", entries)[2]            # alias of a deprecated subject
    assert "no replacement" in subjects.check_new_fact("dead-end", entries)[2]


def test_edit_functions():
    base = subjects.parse({"subjects": [entry("a"), entry("b", aliases=["bee"])]})
    new, changed = subjects.add_alias(base, "a", "ay")
    assert changed and {e["name"]: e["aliases"] for e in new}["a"] == ["ay"] and base[0]["aliases"] == []   # input untouched
    assert subjects.add_alias(new, "a", "ay") == (new, False)
    with pytest.raises(subjects.SubjectsError, match="already belongs to 'b'"):
        subjects.add_alias(new, "a", "bee")
    with pytest.raises(subjects.SubjectsError, match="already a subject name"):
        subjects.add_alias(new, "a", "b")
    with pytest.raises(subjects.SubjectsError, match="unknown subject"):
        subjects.add_alias(new, "ghost", "x")
    with pytest.raises(subjects.SubjectsError, match="kebab-case"):
        subjects.add_alias(new, "a", "Bad Alias")
    # a subject that exists only in the db gets a minimal entry
    new2, changed = subjects.add_alias(base, "dbonly", "dbo", known={"dbonly"}, domain="music")
    assert changed and {e["name"]: e for e in new2}["dbonly"]["domain"] == "music"
    with pytest.raises(subjects.SubjectsError, match="already a subject name"):
        subjects.add_alias(base, "a", "dbonly", known={"dbonly"})
    # describe
    d, changed = subjects.describe(base, "a", "  Scope note ")
    assert changed and {e["name"]: e for e in d}["a"]["description"] == "Scope note"
    assert subjects.describe(d, "a", "Scope note")[1] is False
    cleared, changed = subjects.describe(d, "a", "   ")
    assert changed and {e["name"]: e for e in cleared}["a"]["description"] is None
    assert subjects.describe(base, "a", " ")[1] is False
    # deprecate
    dep, changed = subjects.deprecate(base, "a", "b")
    assert changed and {e["name"]: e for e in dep}["a"]["replaced_by"] == "b"
    assert subjects.deprecate(dep, "a", "b")[1] is False
    with pytest.raises(subjects.SubjectsError, match="cannot replace itself"):
        subjects.deprecate(base, "a", "a")
    with pytest.raises(subjects.SubjectsError, match="unknown replacement"):
        subjects.deprecate(base, "a", "ghost")
    with pytest.raises(subjects.SubjectsError, match="replaced_by cycle"):
        subjects.deprecate(dep, "b", "a")


def test_save_is_atomic_sorted_and_refuses_an_invalid_set(tmp_path):
    p = str(tmp_path / "subjects.json")
    subjects.save(subjects.parse({"subjects": [entry("zeta"), entry("alpha")]}), p)
    assert [e["name"] for e in json.load(open(p))["subjects"]] == ["alpha", "zeta"]
    assert os.listdir(tmp_path) == ["subjects.json"]
    bad = [subjects._entry("a", aliases=["a"])]
    with pytest.raises(subjects.SubjectsError):
        subjects.save(bad, p)
    assert [e["name"] for e in json.load(open(p))["subjects"]] == ["alpha", "zeta"]    # untouched


# ------------------------------------------------------------------ step 06

def write_subjects(env, data):
    os.makedirs(env.data_dir, exist_ok=True)
    with open(os.path.join(env.data_dir, "subjects.json"), "w") as f:
        json.dump(data, f)


def test_06_falls_back_to_the_builtin_table_without_a_file(ingest, capsys):
    con = ingest.db()
    mod = seed_ingested(con)
    mod2 = load06()
    mod2.run(con)
    assert "built-in table; subjects.json not found" in capsys.readouterr().out
    assert sum(1 for p, _ in tree(con).values() if p) == 24


def test_06_loads_everything_from_the_file(ingest, capsys):
    write_subjects(ingest.env, {"subjects": [
        entry("anabolic-steroids", description="Steroids", aliases=["aas", "juice"]),
        entry("aas-legal", parent="anabolic-steroids", relation="part-of", description="The law"),
        entry("old-topic", deprecated=True, replaced_by="anabolic-steroids"),
        entry("brand-new", domain="music")]})
    con = ingest.db()
    con.execute("INSERT INTO subjects (name) VALUES ('anabolic-steroids')")
    con.execute("INSERT INTO subjects (name) VALUES ('aas-legal')")
    load06().run(con)
    r = {x["name"]: x for x in con.execute(
        "SELECT s.*, p.name AS pname, rp.name AS rname FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id "
        "LEFT JOIN subjects rp ON rp.id = s.replaced_by_subject_id")}
    assert r["aas-legal"]["pname"] == "anabolic-steroids" and r["aas-legal"]["parent_relation"] == "part-of"
    assert r["aas-legal"]["description"] == "The law" and r["anabolic-steroids"]["parent_relation"] == "broader"
    assert (r["old-topic"]["deprecated"], r["old-topic"]["rname"]) == (1, "anabolic-steroids")
    assert (r["brand-new"]["domain"], r["anabolic-steroids"]["domain"]) == ("music", "health-and-fitness")
    assert [x[0] for x in con.execute("SELECT alias FROM subject_aliases ORDER BY alias")] == ["aas", "juice"]
    out = capsys.readouterr().out
    assert "(subjects.json)" in out and "2 aliases, 1 deprecated" in out


def test_06_an_existing_subjects_domain_is_kept(ingest):
    write_subjects(ingest.env, {"subjects": [entry("x", domain="music")]})
    con = ingest.db()
    con.execute("INSERT INTO subjects (name, domain) VALUES ('x', 'general')")
    load06().run(con)
    assert con.execute("SELECT domain FROM subjects WHERE name = 'x'").fetchone()[0] == "general"


def test_06_an_invalid_file_fails_the_build(ingest):
    write_subjects(ingest.env, {"subjects": [entry("a", parent="ghost")]})
    with pytest.raises(Exception, match="ghost") as e:
        load06().run(ingest.db())
    assert type(e.value).__name__ == "SubjectsError"


def test_06_an_alias_equal_to_an_ingested_subject_name_fails_the_build(ingest):
    write_subjects(ingest.env, {"subjects": [entry("main", aliases=["already-a-subject"])]})
    con = ingest.db()
    con.execute("INSERT INTO subjects (name) VALUES ('already-a-subject')")
    with pytest.raises(ValueError, match="already-a-subject"):
        load06().run(con)


def test_db_triggers_block_alias_subject_collisions_both_ways():
    con = new_db()
    con.execute("INSERT INTO subjects (name) VALUES ('a')")
    con.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (1, 'ay')")
    with pytest.raises(sqlite3.IntegrityError, match="collides"):
        con.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (1, 'a')")
    with pytest.raises(sqlite3.IntegrityError, match="collides"):
        con.execute("INSERT INTO subjects (name) VALUES ('ay')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (1, 'ay')")      # duplicate alias
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE subjects SET parent_relation = 'kind-of' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE subjects SET replaced_by_subject_id = 1 WHERE id = 1")          # not deprecated / itself


def test_11_files_an_entry_under_its_aliass_canonical_subject(ingest):
    write_subjects(ingest.env, {"subjects": [entry("steroids", aliases=["aas"])]})
    con = ingest.db()
    load06().run(con)
    ingest.run11([F(subject="aas", statement="Filed under the alias.")], con=con)
    assert con.execute("SELECT s.name FROM facts f JOIN subjects s ON s.id = f.subject_id").fetchone()[0] == "steroids"
    assert con.execute("SELECT COUNT(*) FROM subjects WHERE name = 'aas'").fetchone()[0] == 0


# ------------------------------------------------------------------ writers

def nf(**kw):
    import add_fact
    base = dict(statement="S.", subject="x", trust_level="low", no_decay=True, recheck_rationale="r")
    base.update(kw)
    return add_fact.NewFact(**base)


def data_with(tmp_path, data):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    if data is not None:
        (d / "subjects.json").write_text(data if isinstance(data, str) else json.dumps(data))
    return str(d / "general_facts.json")


SUBJ = {"subjects": [entry("steroids", aliases=["aas"]), entry("old", deprecated=True, replaced_by="steroids"),
                     entry("dead", deprecated=True)]}


def test_append_fact_resolves_an_alias_and_reports_it(tmp_path):
    import add_fact
    path = data_with(tmp_path, SUBJ)
    r = add_fact.append_fact(nf(subject="aas"), data_path=path, db_path=str(tmp_path / "none.db"))
    assert r.ok, r.errors
    assert r.entry["subject"] == "steroids" and any("alias of 'steroids'" in n for n in r.notes)
    assert json.load(open(path))[0]["subject"] == "steroids"


def test_append_fact_refuses_a_deprecated_subject_naming_the_replacement(tmp_path):
    import add_fact
    path = data_with(tmp_path, SUBJ)
    r = add_fact.append_fact(nf(subject="old"), data_path=path, db_path=str(tmp_path / "none.db"))
    assert not r.ok and "deprecated" in r.errors[0] and "'steroids'" in r.errors[0] and not os.path.exists(path)
    r = add_fact.append_fact(nf(subject="dead"), data_path=path, db_path=str(tmp_path / "none.db"))
    assert not r.ok and "no replacement" in r.errors[0]


def test_append_fact_without_a_file_or_with_unknown_subjects_is_unchanged(tmp_path):
    import add_fact
    for data in (None, {"subjects": []}):
        path = data_with(tmp_path, data)
        r = add_fact.append_fact(nf(subject="whatever"), data_path=path, db_path=str(tmp_path / "none.db"))
        assert r.ok and r.entry["subject"] == "whatever"
        os.remove(path)


def test_append_fact_with_a_corrupt_file_is_a_clear_error(tmp_path):
    import add_fact
    path = data_with(tmp_path, "{broken")
    r = add_fact.append_fact(nf(), data_path=path, db_path=str(tmp_path / "none.db"))
    assert not r.ok and "not valid JSON" in r.errors[0]


def test_add_facts_batch_resolves_aliases_and_refuses_deprecated(tmp_path):
    import facts_batch
    path = data_with(tmp_path, SUBJ)
    kw = dict(data_dir=os.path.dirname(path), db_path=str(tmp_path / "none.db"), commit=False, dry_run=True)
    res = facts_batch.add_facts([{"statement": "One.", "subject": "aas", "no_decay": True, "recheck_rationale": "r"}], **kw)
    assert res.items[0].subject == "steroids" and res.items[0].outcome == "would_save", res.items[0].errors
    res = facts_batch.add_facts([{"statement": "One.", "subject": "aas", "no_decay": True, "recheck_rationale": "r"},
                                 {"statement": "Two.", "subject": "old", "no_decay": True, "recheck_rationale": "r"}], **kw)
    assert res.items[1].outcome == "invalid" and "'steroids'" in res.items[1].errors[0]
    assert res.items[0].outcome == "not_saved"                                   # all-or-nothing


# ------------------------------------------------------------------ queries

def _query_db():
    con = new_db()
    con.execute("INSERT INTO subjects (id, name) VALUES (1, 'steroids'), (2, 'other')")
    con.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (1, 'aas')")
    for i, (sid, vis) in enumerate([(1, "normal"), (2, "normal"), (1, "private")], start=1):
        con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, visibility, source_key) "
                    "VALUES (?, ?, ?, 'low', 'unreviewed', ?, ?)", (i, sid, f"banana {i}", vis, f"k{i}"))
    return con


def test_subject_filter_accepts_an_alias_in_both_layers():
    import knowledge
    import modes
    con = _query_db()
    assert [r["id"] for r in knowledge.list_facts(con, subject="aas")] == [1, 3]
    assert [r["id"] for r in knowledge.list_facts(con, subject="steroids")] == [1, 3]
    assert [r["id"] for r in knowledge.search_facts(con, "banana", subject="aas")] == [1, 3] or \
        {r["id"] for r in knowledge.search_facts(con, "banana", subject="aas")} == {1, 3}
    assert knowledge.list_facts(con, subject="nope") == []
    normal, private = modes.Session(mode=modes.Mode.normal), modes.Session(mode=modes.Mode.private)
    assert [r["id"] for r in modes.list_facts(normal, con, subject="aas")] == [1]           # fact 3 stays private
    assert [r["id"] for r in modes.list_facts(private, con, subject="aas")] == [1, 3]
    assert modes.list_facts(normal, con, subject="x' OR '1'='1") == []


# ------------------------------------------------------------------ CLI

def cli_env(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    return env


def read_file(env):
    with open(os.path.join(env.data_dir, "subjects.json")) as f:
        return json.load(f)


def test_cli_list_and_show_read_the_built_db(tmp_path):
    env = cli_env(tmp_path)
    env.sql("UPDATE subjects SET description = 'Food', parent_relation = 'part-of' WHERE name = 'protein'")
    env.sql("INSERT INTO subject_aliases (subject_id, alias) VALUES ((SELECT id FROM subjects WHERE name='protein'), 'prot')")
    rows = {r["name"]: r for r in json.loads(env.cli("subject", "list", "--json").stdout)}
    assert rows["protein"]["relation"] == "part-of" and rows["protein"]["parent"] == "nutrition" and rows["protein"]["aliases"] == ["prot"]
    assert rows["nutrition"]["relation"] is None and rows["nutrition"]["n_facts"] >= 1
    out = env.cli("subject", "list").stdout
    assert "protein" in out and "part-of of nutrition" in out and "aka prot" in out
    shown = env.cli("subject", "show", "prot").stdout                 # by alias
    assert "protein" in shown and "(resolved from an alias)" in shown and "Description: Food" in shown and "Parent: nutrition (part-of)" in shown
    j = json.loads(env.cli("subject", "show", "nutrition", "--json").stdout)
    assert j["children"] == ["protein"]
    r = env.cli("subject", "show", "ghost")
    assert r.returncode == 1 and "unknown subject" in r.stderr


def test_cli_edits_write_subjects_json_dry_run_and_idempotence(tmp_path):
    env = cli_env(tmp_path)
    r = env.cli("subject", "alias", "protein", "prot", "--dry-run")
    assert r.returncode == 0 and "dry run" in r.stdout and not os.path.exists(os.path.join(env.data_dir, "subjects.json"))
    r = env.cli("subject", "alias", "protein", "prot")
    assert r.returncode == 0, r.stderr
    assert {e["name"]: e for e in read_file(env)["subjects"]}["protein"]["aliases"] == ["prot"]
    assert "Rebuild to update knowledge.db" in r.stdout
    r = env.cli("subject", "alias", "protein", "prot")
    assert r.returncode == 0 and "no change" in r.stderr + r.stdout
    assert env.cli("subject", "describe", "protein", "Dietary protein").returncode == 0
    assert {e["name"]: e for e in read_file(env)["subjects"]}["protein"]["description"] == "Dietary protein"
    assert env.cli("subject", "deprecate", "jazz", "--replaced-by", "protein").returncode == 0
    d = {e["name"]: e for e in read_file(env)["subjects"]}["jazz"]
    assert d["deprecated"] is True and d["replaced_by"] == "protein"


def test_cli_edit_refusals_exit_one_and_change_nothing(tmp_path):
    env = cli_env(tmp_path)
    assert env.cli("subject", "alias", "protein", "prot").returncode == 0
    before = open(os.path.join(env.data_dir, "subjects.json")).read()
    for args, msg in ((("alias", "ghost", "g"), "unknown subject"),
                      (("alias", "protein", "nutrition"), "already a subject name"),
                      (("alias", "nutrition", "prot"), "already belongs to 'protein'"),
                      (("alias", "protein", "Bad Alias"), "kebab-case"),
                      (("deprecate", "protein", "--replaced-by", "protein"), "cannot replace itself"),
                      (("deprecate", "protein", "--replaced-by", "ghost"), "unknown replacement"),
                      (("describe", "ghost", "x"), "unknown subject")):
        r = env.cli("subject", *args)
        assert r.returncode == 1 and msg in r.stderr, (args, r.stderr)
    assert open(os.path.join(env.data_dir, "subjects.json")).read() == before
    r = env.cli("subject", "alias", "protein", "prot2", "--json")
    assert json.loads(r.stdout)["changed"] is True


def test_cli_edit_on_a_corrupt_file_changes_nothing(tmp_path):
    env = cli_env(tmp_path)
    os.makedirs(env.data_dir, exist_ok=True)
    p = os.path.join(env.data_dir, "subjects.json")
    open(p, "w").write("{broken")
    r = env.cli("subject", "alias", "protein", "prot")
    assert r.returncode == 1 and "not valid JSON" in r.stderr and open(p).read() == "{broken"


def test_cli_edits_commit_only_subjects_json_and_refuse_a_dirty_tree(tmp_path):
    from test_cli_wiring_privacy import GitEnv, git
    g = GitEnv(tmp_path)
    r = g.cli("subject", "alias", "protein", "prot")
    assert r.returncode == 0, r.stderr
    assert g.status() == "" and g.log()[0] == "subjects: alias prot -> protein"
    assert git(g.private, "show", "--name-only", "--format=", "HEAD") == "data/subjects.json"
    open(os.path.join(g.data_dir, "scratch.txt"), "w").write("x")
    head = g.head()
    r = g.cli("subject", "describe", "protein", "text")
    assert r.returncode == 1 and "uncommitted changes" in r.stderr and g.head() == head
    assert g.cli("subject", "describe", "protein", "text", "--allow-dirty").returncode == 0
    assert git(g.private, "show", "--name-only", "--format=", "HEAD") == "data/subjects.json"


# ------------------------------------------------------------------ exporter

def test_export_subjects_dry_run_default_apply_and_no_overwrite(tmp_path):
    import export_subjects
    d = str(tmp_path / "out")
    assert export_subjects.main(["--data-dir", d]) == 0 and not os.path.exists(d)
    assert export_subjects.main(["--data-dir", d, "--apply"]) == 0
    entries = subjects.read_file(os.path.join(d, "subjects.json"))
    assert len(entries) == 26 and sum(1 for e in entries if e["parent"]) == 24
    assert export_subjects.main(["--data-dir", d, "--apply"]) == 1                    # refuses to overwrite
    with pytest.raises(SystemExit):
        export_subjects.main(["--apply", "--dry-run"])


# ------------------------------------------------------------------ normal-only DB

def test_normal_db_carries_subject_metadata_for_included_subjects_only():
    import leak_test
    import normal_db
    import privacy
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        con = sqlite3.connect(path)
        rows = {r[0]: r for r in con.execute("SELECT name, description, parent_relation, deprecated FROM subjects")}
        assert rows["sleep"][2] == "part-of" and rows["sleep"][1] == "Sleep habits and duration"
        assert [r[0] for r in con.execute("SELECT alias FROM subject_aliases")] == ["rest"]
        names = " ".join(str(v) for r in con.execute("SELECT * FROM subjects") for v in r)
        assert leak_test.MARKERS["private subject description"] not in names
        assert not con.execute("SELECT 1 FROM subject_aliases WHERE alias = ?", (leak_test.MARKERS["private subject alias"],)).fetchone()


def test_normal_db_drops_a_description_or_alias_that_contains_a_rule_keyword():
    import leak_test
    import normal_db
    import privacy
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("UPDATE subjects SET description = 'About Zorbak relatives' WHERE name = 'sleep'")
        c.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (2, 'zorbak')")
        c.commit()
        c.close()
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules(keywords=("zorbak",)))
        con = sqlite3.connect(path)
        assert con.execute("SELECT description FROM subjects WHERE name = 'sleep'").fetchone()[0] is None
        assert [r[0] for r in con.execute("SELECT alias FROM subject_aliases")] == ["rest"]
        assert con.execute("SELECT COUNT(*) FROM subjects WHERE name = 'sleep'").fetchone()[0] == 1   # the subject stays


def test_normal_db_keeps_deprecation_and_drops_a_replacement_that_is_not_included():
    import leak_test
    import normal_db
    import privacy
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("UPDATE subjects SET deprecated = 1, replaced_by_subject_id = 3 WHERE name = 'sleep'")   # 3 = private subject
        c.commit()
        c.close()
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        con = sqlite3.connect(path)
        assert tuple(con.execute("SELECT deprecated, replaced_by_subject_id FROM subjects WHERE name = 'sleep'").fetchone()) == (1, None)
