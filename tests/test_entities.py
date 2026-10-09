"""#42: entities as first-class rows (entities.json, step 13, privacy integration, `entity` CLI).

Covers the shared matcher (textmatch), the file rules, deterministic linking, the privacy rule kind
('entity', raise-only), the build step and its invariant, the CLI with its git flow and the keyword
migration, the --entity filters in both query layers, and the normal-only DB. Temp dirs only.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unicodedata

import pytest

import entities_store
import textmatch
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


def ent(id, name, type="person", aliases=(), private=False, **kw):
    d = {"id": id, "canonical_name": name, "type": type}
    if aliases:
        d["aliases"] = list(aliases)
    if private:
        d["private"] = True
    d.update(kw)
    return d


def parse(*es):
    return entities_store.parse({"entities": list(es)})


def new_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    return con


# ------------------------------------------------------------------ textmatch

@pytest.mark.parametrize("text, term, hit", [
    ("my brother called", "brother", True), ("the brotherhood", "brother", False), ("brothers", "brother", False),
    ("Brother, hello", "brother", True), ("my-brother", "brother", True), ("brother's car", "brother", True),
    ("BROTHER", "brother", True), ("a_brother", "brother", False), ("brother2", "brother", False),
    ("mary   jane left", "mary jane", True), ("mary\njane", "mary jane", True), ("maryjane", "mary jane", False),
    ("Zoë Ångström came", "zoë ångström", True), ("ZOË ÅNGSTRÖM", "zoë ångström", True),
    ("uses C++ daily", "c++", True), ("a.b", "a.b", True), ("axb", "a.b", False), ("", "x", False),
])
def test_find_terms_whole_word_literal_case_insensitive(text, term, hit):
    assert bool(textmatch.find_terms(text, [textmatch.normalize_term(term)])) == hit


def test_nfc_and_nfd_text_match_the_same_term():
    term = textmatch.normalize_term("Zoë")
    assert textmatch.find_terms(unicodedata.normalize("NFD", "met Zoë today"), [term])
    assert textmatch.find_terms(unicodedata.normalize("NFC", "met Zoë today"), [term])
    assert textmatch.normalize_term(unicodedata.normalize("NFD", "Zoë")) == term


@pytest.mark.parametrize("bad", [None, 5, "", "   ", "a\x00b", "!!!", "​"])
def test_normalize_term_rejects_unmatchable_terms(bad):
    with pytest.raises(ValueError):
        textmatch.normalize_term(bad)


def test_keywords_and_entity_aliases_agree_on_a_corpus():
    import privacy
    corpus = ["my brother", "brotherhood", "Brother's", "BROTHER!", "a_brother", "bro ther", "the Zoë story", "ZOE", "c++ code",
              "mary  jane", "maryjane", "x-mary jane-y", "", "brother" * 3]
    for term in ("brother", "zoë", "c++", "mary jane"):
        kw = privacy.Rules(keywords=(privacy._normalize_keyword(term),))
        en = privacy.Rules(entities=(("E", (textmatch.normalize_term(term),)),))
        for text in corpus:
            a = privacy.resolve_visibility("s", text, "normal", kw).visibility
            b = privacy.resolve_visibility("s", text, "normal", en).visibility
            assert a == b, (term, text)


# ------------------------------------------------------------------ the file rules

def test_round_trip_and_defaults():
    es = parse(ent("zed", "Zed Name", aliases=["Zeddy", "ZN"], private=True, external_id="wikidata:Q1", notes="n"),
               ent("acme", "Acme", type="organization"))
    data = entities_store.to_json(es)
    assert [e["id"] for e in data["entities"]] == ["acme", "zed"]
    assert data["entities"][0] == {"id": "acme", "canonical_name": "Acme", "type": "organization"}
    assert data["entities"][1]["private"] is True and data["entities"][1]["aliases"] == ["Zeddy", "ZN"]
    assert entities_store.parse(json.loads(json.dumps(data))) == entities_store.parse(data)


@pytest.mark.parametrize("bad, msg", [
    ([], "expected an object"), ({"entities": 3}, "'entities' list"), ({"version": 9, "entities": []}, "unsupported version"),
    ({"entities": [3]}, "not an object"),
    ({"entities": [ent("Bad Id", "x")]}, "kebab-case slug"), ({"entities": [{"canonical_name": "x", "type": "person"}]}, "kebab-case slug"),
    ({"entities": [ent("a", "x"), ent("a", "y")]}, "listed twice"),
    ({"entities": [ent("a", "x", colour="red")]}, "unknown key"),
    ({"entities": [ent("a", "  ")]}, "canonical_name is required"),
    ({"entities": [{"id": "a", "type": "person"}]}, "canonical_name is required"),
    ({"entities": [ent("a", "x", type="alien")]}, "type"),
    ({"entities": [dict(ent("a", "x"), aliases="y")]}, "aliases must be a list"),
    ({"entities": [ent("a", "x", aliases=["  "])]}, "alias is blank"),
    ({"entities": [ent("a", "x", aliases=[5])]}, "alias must be text"),
    ({"entities": [ent("a", "x", aliases=["!!!"])]}, "no letters or digits"),
    ({"entities": [dict(ent("a", "x"), private="yes")]}, "private must be true or false"),
    ({"entities": [dict(ent("a", "x"), notes=3)]}, "notes must be text"),
    ({"entities": [ent("a", "Same"), ent("b", "same")]}, "claimed twice"),
    ({"entities": [ent("a", "Mary", aliases=["MJ"]), ent("b", "Other", aliases=["mj"])]}, "claimed twice"),
    ({"entities": [ent("a", "Mary"), ent("b", "Other", aliases=["MARY"])]}, "claimed twice"),
    ({"entities": [ent("a", "Mary", aliases=["MJ", "mj"])]}, "same alias is listed twice"),
])
def test_invalid_files_are_refused(bad, msg):
    with pytest.raises(entities_store.EntitiesError, match=msg):
        entities_store.parse(bad)


def test_read_file_absent_corrupt_and_save_refuses_invalid(tmp_path):
    assert entities_store.read_file(str(tmp_path / "e.json")) is None
    p = tmp_path / "e.json"
    p.write_text("{nope")
    with pytest.raises(entities_store.EntitiesError, match="not valid JSON"):
        entities_store.read_file(str(p))
    good = parse(ent("a", "A"))
    entities_store.save(good, str(p))
    assert os.listdir(tmp_path) == ["e.json"]
    bad = [entities_store.make_entity("a", "Same", "person"), entities_store.make_entity("b", "SAME", "person")]
    with pytest.raises(entities_store.EntitiesError):
        entities_store.save(bad, str(p))
    assert entities_store.read_file(str(p)) == good


# ------------------------------------------------------------------ matching

def test_mentions_whole_word_and_aliases():
    e = parse(ent("bro", "Brother", aliases=["bro", "Big B"]))[0]
    assert entities_store.mentions("my brother said hi", e) == ["brother"]
    assert entities_store.mentions("the brotherhood", e) == []
    assert entities_store.mentions("BIG   b arrived", e) == ["big b"]
    assert entities_store.mentions("", e) == [] and entities_store.mentions(None, e) == []
    assert entities_store.terms(e) == ("brother", "bro", "big b")


def test_match_all_scans_every_text_and_ignores_none():
    es = parse(ent("a", "Alice"), ent("b", "Bob"), ent("c", "Carol"))
    hits = entities_store.match_all(["met alice", None, "", "Bob's note"], es)
    assert hits == {"a": ["alice"], "b": ["bob"]}
    assert entities_store.match_all([], es) == {} and entities_store.match_all([None], es) == {}


def test_find_by_id_name_or_alias():
    es = parse(ent("zed", "Zed Name", aliases=["Zeddy"]))
    for ref in ("zed", "Zed Name", "zed  NAME", "zeddy", "ZEDDY"):
        assert entities_store.find(es, ref)["id"] == "zed", ref
    for ref in ("nobody", "", "  ", None, 5):
        assert entities_store.find(es, ref) is None, ref


def test_edits():
    es = parse(ent("a", "Alice"))
    new, changed = entities_store.add_entity(es, "Bob Builder", "person", ["Bobby"], private=True, notes="n")
    assert changed and [e["id"] for e in new] == ["a", "bob-builder"] and new[1]["private"] and len(es) == 1
    assert entities_store.add_entity(new, "Bob Builder 2", "person")[0][2]["id"] == "bob-builder-2"
    assert entities_store.unique_id("Zoë!", set()) == "zoe" and entities_store.unique_id("???", set()) == "entity"
    assert entities_store.unique_id("Alice", {"alice"}) == "alice-2"
    with pytest.raises(entities_store.EntitiesError, match="claimed twice"):
        entities_store.add_entity(new, "bobby")                                        # collides with an alias
    with pytest.raises(entities_store.EntitiesError):
        entities_store.add_entity(new, "   ")
    with pytest.raises(entities_store.EntitiesError, match="type"):
        entities_store.add_entity(new, "Zed", "alien")
    n2, c2 = entities_store.add_alias(new, "alice", "Al")
    assert c2 and n2[0]["aliases"] == ["Al"] and new[0]["aliases"] == []
    assert entities_store.add_alias(n2, "alice", "AL")[1] is False                      # already there (normalized)
    with pytest.raises(entities_store.EntitiesError, match="claimed twice"):
        entities_store.add_alias(n2, "alice", "Bobby")
    with pytest.raises(entities_store.EntitiesError, match="unknown entity"):
        entities_store.add_alias(n2, "ghost", "x")
    t, ct = entities_store.set_private(es, "Alice", True)
    assert ct and t[0]["private"] and entities_store.set_private(t, "alice", True)[1] is False
    assert entities_store.set_private(t, "alice", False)[0][0]["private"] is False
    with pytest.raises(entities_store.EntitiesError):
        entities_store.set_private(es, "ghost", True)


# ------------------------------------------------------------------ privacy integration

def write_entities(data_dir, *es):
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "entities.json"), "w") as f:
        json.dump({"version": 1, "entities": list(es)}, f)


def test_load_rules_reads_only_private_entities_and_fails_closed(tmp_path):
    import privacy
    import privacy_store
    d = str(tmp_path / "data")
    rules_file = os.path.join(d, "privacy_rules.json")
    assert privacy_store.load_rules(rules_file).entities == ()                          # nothing at all
    write_entities(d, ent("zed", "Zed Name", aliases=["Zeddy"], private=True), ent("pub", "Public Co", type="organization"))
    r = privacy_store.load_rules(rules_file)
    assert r.entities == (("Zed Name", ("zed name", "zeddy")),) and r.keywords == ()
    with open(rules_file, "w") as f:
        json.dump({"version": 1, "keywords": ["k"]}, f)
    assert privacy_store.load_rules(rules_file).keywords == ("k",) and len(privacy_store.load_rules(rules_file).entities) == 1
    open(os.path.join(d, "entities.json"), "w").write("{broken")
    with pytest.raises(privacy.PrivacyRulesError, match="not valid JSON"):
        privacy_store.load_rules(rules_file)
    write_entities(d, ent("a", "Same"), ent("b", "SAME"))
    with pytest.raises(privacy.PrivacyRulesError, match="claimed twice"):
        privacy_store.load_rules(rules_file)


def test_save_rules_never_writes_entities(tmp_path):
    import privacy
    import privacy_store
    d = str(tmp_path / "data")
    write_entities(d, ent("zed", "Zed Name", private=True))
    rf = os.path.join(d, "privacy_rules.json")
    rules = privacy_store.load_rules(rf)
    privacy_store.save_rules(rules, rf)
    assert "Zed Name" not in open(rf).read() and json.load(open(rf)).keys() == {"version", "subject_tags", "keywords"}


def test_resolver_entity_rule_is_raise_only_and_names_the_entity():
    import privacy
    rules = privacy.Rules(entities=(("Zed Name", ("zed name", "zeddy")),))
    r = privacy.resolve_visibility("s", "Went hiking with Zeddy.", "normal", rules)
    assert r.visibility == "private" and [x.kind for x in r.raised_by] == ["entity"]
    assert "'Zed Name'" in r.explain() and "statement" in r.explain()
    r = privacy.resolve_visibility("s", "Harmless.", "normal", rules, extra_text=(None, "from a chat with ZED NAME"))
    assert r.visibility == "private" and "another field" in r.explain()
    assert privacy.resolve_visibility("s", "Harmless.", "normal", rules).visibility == "normal"
    assert privacy.resolve_visibility("s", "Harmless.", "private", rules).visibility == "private"          # never lowers
    assert privacy.resolve_visibility("s", "the zeddyhood", "normal", rules).visibility == "normal"        # whole word
    both = privacy.Rules(keywords=("zeddy",), entities=rules.entities)
    assert {x.kind for x in privacy.resolve_visibility("s", "zeddy", "normal", both).raised_by} == {"keyword", "entity"}


def test_apply_rules_to_db_raises_facts_that_mention_a_private_entity():
    import privacy
    import privacy_store
    con = new_db()
    con.execute("INSERT INTO subjects (id, name) VALUES (1, 's')")
    for i, (stmt, notes) in enumerate([("Went with Zeddy.", None), ("Plain.", "note about Zed Name"), ("Plain too.", None)], start=1):
        con.execute("INSERT INTO facts (id, subject_id, statement, notes, trust_level, freshness, visibility, source_key) "
                    "VALUES (?, 1, ?, ?, 'low', 'unreviewed', 'normal', ?)", (i, stmt, notes, f"k{i}"))
    rules = privacy.Rules(entities=(("Zed Name", ("zed name", "zeddy")),))
    out = privacy_store.apply_rules_to_db(con, rules)
    assert sorted(f for f, _ in out["raised"]) == [1, 2]
    assert [r[0] for r in con.execute("SELECT visibility FROM facts ORDER BY id")] == ["private", "private", "normal"]


def test_add_fact_stores_private_when_it_mentions_a_private_entity(tmp_path):
    import add_fact
    d = tmp_path / "data"
    write_entities(str(d), ent("zed", "Zed Name", aliases=["Zeddy"], private=True))
    fact = add_fact.NewFact(statement="Lunch with zeddy.", subject="x", trust_level="low", no_decay=True, recheck_rationale="r", visibility="normal")
    res = add_fact.resolve_privacy(fact, str(d / "general_facts.json"), str(tmp_path / "none.db"))
    assert res.visibility == "private" and "Zed Name" in res.explain()


# ------------------------------------------------------------------ build step 13

def load13():
    spec = importlib.util.spec_from_file_location("link13", os.path.join(REPO, "13_link_entities.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_step13_links_facts_and_writes_the_tables(ingest, capsys):
    write_entities(ingest.env.data_dir,
                   ent("zed", "Zed Name", aliases=["Zeddy"], private=True),
                   ent("acme", "Acme Labs", type="organization", aliases=["Acme"], external_id="wikidata:Q1", notes="a lab"),
                   ent("lonely", "Nobody Mentions Me"),
                   ent("bro", "brother"))
    con = ingest.run04([F(statement="Lunch with Zeddy at Acme.", visibility="normal"),
                        F(statement="Plain.", notes="see Acme Labs", visibility="normal"),
                        F(statement="My brotherhood is strong.", visibility="normal"),
                        F(statement="Call my brother.", visibility="normal")])
    mod13 = load13()
    mod13.DATA_DIR = ingest.env.data_dir
    mod13.run(con)
    assert "4 entities (1 private), 4 fact links" in capsys.readouterr().out
    links = {(r[0], r[1]) for r in con.execute("SELECT f.statement, e.entity_key FROM fact_entities fe JOIN facts f ON f.id = fe.fact_id JOIN entities e ON e.id = fe.entity_id")}
    assert links == {("Lunch with Zeddy at Acme.", "zed"), ("Lunch with Zeddy at Acme.", "acme"), ("Plain.", "acme"), ("Call my brother.", "bro")}
    assert con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 4                       # 'lonely' has no facts but exists
    assert [r[0] for r in con.execute("SELECT alias FROM entity_aliases ORDER BY alias")] == ["Acme", "Zeddy"]
    e = con.execute("SELECT * FROM entities WHERE entity_key = 'acme'").fetchone()
    assert (e["type"], e["external_id"], e["notes"], e["private"], e["name_norm"]) == ("organization", "wikidata:Q1", "a lab", 0, "acme labs")


def test_step13_without_a_file_does_nothing(ingest, capsys):
    con = ingest.run04([F()])
    mod13 = load13()
    mod13.DATA_DIR = ingest.env.data_dir
    mod13.run(con)
    assert "no entities.json" in capsys.readouterr().out
    assert con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0


def test_step13_fails_the_build_on_a_name_collision_and_on_a_bad_file(ingest):
    mod13 = load13()
    mod13.DATA_DIR = ingest.env.data_dir
    con = ingest.run04([F()])                       # no entities.json yet
    write_entities(ingest.env.data_dir, ent("a", "Same", aliases=["x"]), ent("b", "Other", aliases=["X"]))
    with pytest.raises(Exception, match="claimed twice") as e:
        mod13.run(con)
    assert type(e.value).__name__ == "EntitiesError"
    open(os.path.join(ingest.env.data_dir, "entities.json"), "w").write("{")
    with pytest.raises(Exception, match="not valid JSON"):
        mod13.run(con)
    # and the privacy pass in 04/11 refuses to run at all on a broken entity file (fail closed)
    with pytest.raises(Exception, match="not valid JSON") as e:
        ingest.run04([F()])
    assert type(e.value).__name__ == "PrivacyRulesError"


def test_a_rebuild_privatizes_old_facts_end_to_end(ingest):
    """04 applies the rules (with the entity) and step 13 agrees: a normal-requested fact that mentions a private entity ends up private and linked."""
    write_entities(ingest.env.data_dir, ent("zed", "Zed Name", private=True))
    con = ingest.run04([F(statement="Lunch with Zed Name.", visibility="normal")])
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "private"
    mod13 = load13()
    mod13.DATA_DIR = ingest.env.data_dir
    mod13.run(con)
    assert con.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0] == 1


def test_step13_refuses_to_build_if_a_fact_mentioning_a_private_entity_is_not_private(ingest):
    """The invariant check: simulate a privacy pass that missed (no entity rule loaded, e.g. a bug) by linking directly."""
    con = ingest.run04([F(statement="Lunch with Zed Name.", visibility="normal")])      # no entities.json yet: stays normal
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "normal"
    write_entities(ingest.env.data_dir, ent("zed", "Zed Name", private=True))
    mod13 = load13()
    mod13.DATA_DIR = ingest.env.data_dir
    with pytest.raises(ValueError, match="not stored private"):
        mod13.run(con)


# ------------------------------------------------------------------ queries

def _linked_db():
    con = new_db()
    con.execute("INSERT INTO subjects (id, name) VALUES (1, 's')")
    con.execute("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type, private) VALUES (1, 'acme', 'Acme Labs', 'acme labs', 'organization', 0)")
    con.execute("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type, private) VALUES (2, 'zed', 'Zed Name', 'zed name', 'person', 1)")
    con.execute("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (1, 'Acme', 'acme')")
    for i, (stmt, vis, eids) in enumerate([("banana at Acme", "normal", [1]), ("banana alone", "normal", []),
                                           ("banana with Zed", "private", [2]), ("banana both", "private", [1, 2])], start=1):
        con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, visibility, source_key) "
                    "VALUES (?, 1, ?, 'low', 'unreviewed', ?, ?)", (i, stmt, vis, f"k{i}"))
        for e in eids:
            con.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (i, e))
    return con


def test_knowledge_entity_filter_by_name_alias_and_id():
    import knowledge
    con = _linked_db()
    for ref in ("Acme Labs", "acme", "ACME", "  acme   labs ", "acme"):
        assert [r["id"] for r in knowledge.list_facts(con, entity=ref)] == [1, 4], ref
    assert [r["id"] for r in knowledge.list_facts(con, entity="zed")] == [3, 4]
    assert [r["id"] for r in knowledge.search_facts(con, "banana", entity="Acme")] in ([1, 4], [4, 1])
    assert knowledge.list_facts(con, entity="nobody") == []
    assert [r["id"] for r in knowledge.list_facts(con)] == [1, 2, 3, 4]
    for bad in ("", "   ", "!!!"):
        with pytest.raises(ValueError):
            knowledge.list_facts(con, entity=bad)
    assert knowledge.list_facts(con, entity="x' OR '1'='1") == []


def test_modes_entity_filter_respects_mode():
    import modes
    import modes_store
    con = _linked_db()
    normal, private = modes.Session(mode=modes.Mode.normal), modes.Session(mode=modes.Mode.private)
    assert [r["id"] for r in modes_store.list_facts(normal, con, entity="acme")] == [1]               # fact 4 is private
    assert [r["id"] for r in modes_store.list_facts(private, con, entity="acme")] == [1, 4]
    assert modes_store.list_facts(normal, con, entity="zed") == []                                    # naming a private entity reveals nothing
    assert [r["id"] for r in modes_store.list_facts(private, con, entity="zed")] == [3, 4]
    assert [r["id"] for r in modes_store.search_facts(normal, con, "banana", entity="Acme")] == [1]
    with pytest.raises(ValueError):
        modes_store.list_facts(normal, con, entity="  ")


# ------------------------------------------------------------------ CLI

def cli_env(tmp_path):
    from test_cli_wiring import Env
    return Env(tmp_path)


def efile(env):
    with open(os.path.join(env.data_dir, "entities.json")) as f:
        return {e["id"]: e for e in json.load(f)["entities"]}


def test_cli_add_alias_tag_untag_roundtrip(tmp_path):
    env = cli_env(tmp_path)
    r = env.cli("entity", "add", "Zed Name", "--type", "person", "--alias", "Zeddy", "--private", "--notes", "my cousin", "--external-id", "x:1")
    assert r.returncode == 0, r.stderr
    e = efile(env)["zed-name"]
    assert (e["type"], e["private"], e["aliases"], e["notes"], e["external_id"]) == ("person", True, ["Zeddy"], "my cousin", "x:1")
    assert env.cli("entity", "alias", "zeddy", "ZN").returncode == 0 and efile(env)["zed-name"]["aliases"] == ["Zeddy", "ZN"]
    assert env.cli("entity", "untag", "ZN").returncode == 0 and "private" not in efile(env)["zed-name"]
    assert env.cli("entity", "tag", "zed-name").returncode == 0 and efile(env)["zed-name"]["private"] is True
    r = env.cli("entity", "tag", "zed-name")
    assert r.returncode == 0 and "no change" in r.stdout + r.stderr


def test_cli_refusals_and_dry_run_change_nothing(tmp_path):
    env = cli_env(tmp_path)
    assert env.cli("entity", "add", "Alice", "--alias", "Al").returncode == 0
    before = open(os.path.join(env.data_dir, "entities.json")).read()
    for args, msg in ((("add", "al"), "claimed twice"), (("add", "  "), "canonical_name is required"),
                      (("alias", "ghost", "g"), "unknown entity"), (("alias", "alice", "AL"), None),
                      (("tag", "ghost"), "unknown entity"), (("add", "X", "--type", "alien"), None)):
        r = env.cli("entity", *args)
        if msg:
            assert r.returncode == 1 and msg in r.stderr, (args, r.stderr)
    assert env.cli("entity", "add", "X", "--type", "alien").returncode == 2
    assert open(os.path.join(env.data_dir, "entities.json")).read() == before
    r = env.cli("entity", "add", "Bob", "--dry-run")
    assert r.returncode == 0 and "dry run" in r.stdout and open(os.path.join(env.data_dir, "entities.json")).read() == before


def test_cli_list_and_show(tmp_path):
    env = cli_env(tmp_path)
    assert "No entities" in env.cli("entity", "list").stdout
    env.cli("entity", "add", "Zed Name", "--alias", "Zeddy", "--private")
    env.cli("entity", "add", "Acme Labs", "--type", "organization")
    rows = json.loads(env.cli("entity", "list", "--json").stdout)
    assert [r["id"] for r in rows] == ["acme-labs", "zed-name"] and rows[1]["private"] is True and rows[0]["n_facts"] is None
    out = env.cli("entity", "list").stdout
    assert "zed-name  Zed Name" in out and "PRIVATE" in out and "aka Zeddy" in out
    shown = env.cli("entity", "show", "zeddy").stdout
    assert "Zed Name  (zed-name)" in shown and "PRIVATE" in shown and "Aliases: Zeddy" in shown
    assert json.loads(env.cli("entity", "show", "acme-labs", "--json").stdout)["type"] == "organization"
    r = env.cli("entity", "show", "nobody")
    assert r.returncode == 1 and "unknown entity" in r.stderr


def test_cli_list_counts_links_from_the_built_db(tmp_path):
    env = cli_env(tmp_path)
    env.cli("entity", "add", "Acme Labs")
    con = sqlite3.connect(env.db)
    con.execute("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type) VALUES (1, 'acme-labs', 'Acme Labs', 'acme labs', 'other')")
    con.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (1, 1), (2, 1)")
    con.commit()
    con.close()
    assert json.loads(env.cli("entity", "list", "--json").stdout)[0]["n_facts"] == 2
    assert "2 facts" in env.cli("entity", "list").stdout


def test_cli_search_and_facts_filter_by_entity(tmp_path):
    env = cli_env(tmp_path)
    con = sqlite3.connect(env.db)
    con.execute("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type) VALUES (1, 'acme-labs', 'Acme Labs', 'acme labs', 'other')")
    con.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (1, 1)")
    con.commit()
    con.close()
    assert [r["id"] for r in json.loads(env.cli("facts", "--entity", "acme labs", "--json").stdout)] == [1]
    assert [r["id"] for r in json.loads(env.cli("search", "protein", "--entity", "ACME-LABS", "--json").stdout)] == [1]
    assert json.loads(env.cli("facts", "--entity", "nobody", "--json").stdout) == []
    assert env.cli("facts", "--entity", "  ").returncode == 1


def test_cli_edits_commit_only_entities_json_and_refuse_a_dirty_tree(tmp_path):
    from test_cli_wiring_privacy import GitEnv, git
    g = GitEnv(tmp_path)
    r = g.cli("entity", "add", "Zed Name", "--private")
    assert r.returncode == 0, r.stderr
    assert g.status() == "" and g.log()[0] == "entities: add Zed Name"
    assert git(g.private, "show", "--name-only", "--format=", "HEAD") == "data/entities.json"
    open(os.path.join(g.data_dir, "scratch.txt"), "w").write("x")
    head = g.head()
    r = g.cli("entity", "alias", "zed-name", "Zeddy")
    assert r.returncode == 1 and "uncommitted changes" in r.stderr and g.head() == head
    assert g.cli("entity", "alias", "zed-name", "Zeddy", "--allow-dirty").returncode == 0


# ------------------------------------------------------------------ keyword migration

def put_rules(env, **rules):
    os.makedirs(env.data_dir, exist_ok=True)
    with open(env.rules_file(), "w") as f:
        json.dump({"version": 1, **rules}, f)


def test_migrate_keywords_to_private_entities_in_one_commit(tmp_path):
    from test_cli_wiring_privacy import GitEnv, git
    g = GitEnv(tmp_path)
    put_rules(g, keywords=["mary jane", "zed"], subject_tags={"family": "private"})
    git(g.private, "add", "-A")
    git(g.private, "commit", "-q", "-m", "rules")
    before = g.cli("privacy", "check", "Lunch with Mary Jane.", "--subject", "nutrition").stdout
    assert before.startswith("private")
    r = g.cli("entity", "migrate-keywords")
    assert r.returncode == 0, r.stderr
    es = efile(g)
    assert set(es) == {"mary-jane", "zed"} and all(e["private"] and e["type"] == "other" for e in es.values())
    assert g.rules()["keywords"] == [] and g.rules()["subject_tags"] == {"family": "private"}
    assert g.status() == ""
    assert sorted(git(g.private, "show", "--name-only", "--format=", "HEAD").splitlines()) == ["data/entities.json", "data/privacy_rules.json"]
    after = g.cli("privacy", "check", "Lunch with Mary Jane.", "--subject", "nutrition").stdout
    assert after.startswith("private") and "Mary Jane".casefold() in after.casefold()
    assert g.cli("privacy", "check", "Lunch with Maryjane.", "--subject", "nutrition").stdout.startswith("normal")
    again = g.cli("entity", "migrate-keywords")
    assert again.returncode == 0 and "no change" in again.stdout + again.stderr


def test_migrate_keep_keywords_and_existing_private_entity(tmp_path):
    env = cli_env(tmp_path)
    put_rules(env, keywords=["zed"])
    env.cli("entity", "add", "Zed", "--private")
    r = env.cli("entity", "migrate-keywords")
    assert r.returncode == 0 and env.rules()["keywords"] == [] and list(efile(env)) == ["zed"]          # covered: just dropped
    put_rules(env, keywords=["bob"])
    assert env.cli("entity", "migrate-keywords", "--keep-keywords").returncode == 0
    assert env.rules()["keywords"] == ["bob"] and efile(env)["bob"]["private"] is True


def test_migrate_refuses_when_a_keyword_names_a_non_private_entity(tmp_path):
    env = cli_env(tmp_path)
    env.cli("entity", "add", "Zed Name", "--alias", "zed")
    put_rules(env, keywords=["zed"])
    before = (open(env.rules_file()).read(), open(os.path.join(env.data_dir, "entities.json")).read())
    r = env.cli("entity", "migrate-keywords")
    assert r.returncode == 1 and "non-private entity" in r.stderr and "tag it private first" in r.stderr
    assert (open(env.rules_file()).read(), open(os.path.join(env.data_dir, "entities.json")).read()) == before


def test_migrate_dry_run_writes_nothing_and_no_keywords_is_no_change(tmp_path):
    env = cli_env(tmp_path)
    put_rules(env, keywords=["zed"])
    before = open(env.rules_file()).read()
    r = env.cli("entity", "migrate-keywords", "--dry-run")
    assert r.returncode == 0 and "dry run" in r.stdout and open(env.rules_file()).read() == before
    assert not os.path.exists(os.path.join(env.data_dir, "entities.json"))
    put_rules(env, keywords=[])
    assert "no change" in (lambda x: x.stdout + x.stderr)(env.cli("entity", "migrate-keywords"))


# ------------------------------------------------------------------ normal-only DB

def build_normal(full, d, rules=None):
    import normal_db
    import privacy
    path, _ = normal_db.build_normal_atomic(full, d, rules or privacy.Rules())
    return sqlite3.connect(path), path


def test_normal_db_excludes_private_entities_and_includes_public_ones():
    import leak_test
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        con, path = build_normal(full, d)
        assert [tuple(r) for r in con.execute("SELECT entity_key, canonical_name, type, external_id, notes FROM entities")] == \
            [("acme-labs", "Acme Labs", "organization", "wikidata:Q1", "A public lab")]
        assert [tuple(r) for r in con.execute("SELECT entity_id, alias FROM entity_aliases")] == [(2, "Acme")]
        assert [tuple(r) for r in con.execute("SELECT fact_id, entity_id FROM fact_entities")] == [(1, 2)]
        raw = open(path, "rb").read()
        for key in ("private entity name", "private entity alias", "private entity notes"):
            assert leak_test.MARKERS[key].encode() not in raw, key
        assert b"quillon-fernsby" not in raw
        assert not leak_test.scan_against_full(path, full)        # the leak test itself finds nothing, rows or raw bytes


def test_leak_test_flags_an_injected_private_entity_marker():
    import leak_test
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        con, path = build_normal(full, d)
        con.execute("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (2, ?, 'planted')", (leak_test.MARKERS["private entity alias"],))
        con.commit()
        con.close()
        assert leak_test.scan_against_full(path, full)


def test_normal_db_drops_entity_text_that_trips_a_keyword_and_skips_a_named_entity():
    import leak_test
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("UPDATE entities SET notes = 'Zorbak relatives lab' WHERE id = 2")
        c.execute("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (2, 'zorbak labs', 'zorbak labs')")
        c.commit()
        c.close()
        con, _ = build_normal(full, d, privacy.Rules(keywords=("zorbak",)))
        assert con.execute("SELECT notes FROM entities WHERE id = 2").fetchone()[0] is None
        assert sorted(r[0] for r in con.execute("SELECT alias FROM entity_aliases")) == ["Acme"]
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        con, _ = build_normal(full, d, privacy.Rules(keywords=("acme",)))               # the NAME trips the rule: entity left out
        assert con.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0] == 0


def test_normal_db_never_copies_a_private_entity_even_if_linked_to_an_included_fact():
    import leak_test
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (1, 1)")        # normal fact 1 "linked" to the private entity
        c.commit()
        c.close()
        con, _ = build_normal(full, d)
        assert [r[0] for r in con.execute("SELECT entity_key FROM entities")] == ["acme-labs"]
        assert [tuple(r) for r in con.execute("SELECT * FROM fact_entities")] == [(1, 2)]
