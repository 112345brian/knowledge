"""#31: deterministic privacy rules. Synthetic names only, and only in this file: the public repo
must never contain real names, and a test below greps the tracked files to prove the fixtures
live here and nowhere else."""
import ast
import json
import os
import sqlite3
import subprocess

import pytest

from test_add_fact import REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)

import privacy
from privacy import Rules, resolve_visibility as rv

SYNTHETIC_NAMES = ["Zorblax", "Quenby", "Wumpfel"]


def R(tags=None, kws=(), parents=None, known=None):
    return Rules(subject_tags=dict(tags or {}), keywords=tuple(privacy._normalize_keyword(k) for k in kws),
                 parents=dict(parents or {}), known_subjects=None if known is None else frozenset(known))


# ----------------------------------------------------------------- the resolver

def test_private_tagged_subject_is_private_whatever_is_requested():
    rules = R(tags={"family": "private"})
    for requested in ("private", "normal"):
        assert rv("family", "Nothing sensitive.", requested, rules).visibility == "private"
    assert [r.kind for r in rv("family", "x", "normal", rules).raised_by] == ["subject-tag"]


def test_tag_on_a_parent_applies_to_descendants_and_says_so():
    rules = R(tags={"family": "private"}, parents={"cousins": "family", "far": "cousins", "family": None})
    res = rv("far", "x", "normal", rules)
    assert res.visibility == "private"
    assert "'family'" in res.explain() and "'far'" in res.explain()


def test_a_child_tag_does_not_leak_up_to_the_parent():
    rules = R(tags={"secret": "private"}, parents={"secret": "general", "general": None})
    assert rv("general", "x", "normal", rules).visibility == "normal"


def test_keyword_hit_raises_normal_to_private():
    res = rv("s", "I called Zorblax today.", "normal", R(kws=["zorblax"]))
    assert res.visibility == "private"
    assert res.raised_by[0].kind == "keyword" and "zorblax" in res.explain()


def test_no_input_can_lower_visibility():
    assert rv("s", "plain", "private", R()).visibility == "private"
    assert rv("s", "plain", "normal", R()).visibility == "normal"
    for bad in ("public", None, ["normal"], ""):
        with pytest.raises(ValueError):
            rv("s", "plain", bad, R())


def test_a_normal_tag_never_lowers_a_private_parent_or_a_keyword():
    rules = R(tags={"p": "private", "c": "normal"}, parents={"c": "p", "p": None}, kws=["quenby"])
    assert rv("c", "x", "normal", rules).visibility == "private"
    assert rv("c", "x", "private", R(tags={"c": "normal"})).visibility == "private"
    assert rv("c", "Quenby", "normal", R(tags={"c": "normal"}, kws=["quenby"])).visibility == "private"


def test_unknown_subject_is_private_when_the_subject_list_is_known():
    rules = R(known=["real"])
    assert rv("ghost", "x", "normal", rules).visibility == "private"
    assert rv("ghost", "x", "normal", rules).raised_by[0].kind == "unknown-subject"
    assert rv("real", "x", "normal", rules).visibility == "normal"


def test_subject_registered_with_a_normal_tag_counts_as_known():
    assert rv("new", "x", "normal", R(tags={"new": "normal"}, known=["real"])).visibility == "normal"


@pytest.mark.parametrize("subject", [None, "", "   ", 5])
def test_missing_or_blank_subject_fails_closed(subject):
    assert rv(subject, "x", "normal", R()).visibility == "private"


def test_cycle_in_the_subject_tree_fails_closed_and_terminates():
    assert rv("a", "x", "normal", R(parents={"a": "b", "b": "a"})).visibility == "private"


@pytest.mark.parametrize("statement,hit", [
    ("my brother called", True),
    ("The brotherhood met", False),
    ("brothers", False),
    ("BROTHER!", True),
    ("(brother)", True),
    ("brother's car", True),
    ("half-brother", True),
    ("brother_in_law", False),
    ("brother2", False),
    ("my  Brother", True),
    ("", False),
    (None, False),
])
def test_whole_word_matching(statement, hit):
    assert (rv("s", statement, "normal", R(kws=["brother"])).visibility == "private") is hit


def test_brotherhood_matches_only_if_listed():
    assert rv("s", "the brotherhood", "normal", R(kws=["brother", "brotherhood"])).visibility == "private"


@pytest.mark.parametrize("statement", ["Zoë was here", "zoë was here", "ZOË was here", "Zoë was here"])
def test_unicode_names_match_case_insensitively_and_across_normalization_forms(statement):
    assert rv("s", statement, "normal", R(kws=["Zoë"])).visibility == "private"


def test_multi_word_keyword_matches_any_whitespace_run_and_whole_words():
    rules = R(kws=["best friend"])
    assert rv("s", "my best   friend", "normal", rules).visibility == "private"
    assert rv("s", "my best\nfriend", "normal", rules).visibility == "private"
    assert rv("s", "my best friends", "normal", rules).visibility == "normal"


@pytest.mark.parametrize("kw,hit,miss", [
    ("a.b", "see a.b now", "see axb now"),
    ("c++", "uses c++ daily", "uses cpp daily"),
    ("(x)", "the (x) case", "the case"),
    ("a|b", "a|b", "a"),
    ("a*b", "a*b", "aab"),
])
def test_regex_metacharacters_in_rules_are_literal(kw, hit, miss):
    rules = R(kws=[kw])
    assert rv("s", hit, "normal", rules).visibility == "private"
    assert rv("s", miss, "normal", rules).visibility == "normal"


def test_very_long_statement_is_fast_and_correct():
    text = ("lorem ipsum " * 200_000) + "Wumpfel"
    assert rv("s", text, "normal", R(kws=["wumpfel"])).visibility == "private"
    assert rv("s", text[:-7], "normal", R(kws=["wumpfel"])).visibility == "normal"


def test_empty_rules_change_nothing():
    res = rv("s", "anything at all", "normal", R())
    assert res.visibility == "normal" and res.explain().startswith("normal")


def test_the_result_explains_itself():
    res = rv("family", "Zorblax and Quenby", "normal", R(tags={"family": "private"}, kws=["zorblax", "quenby"]))
    assert [r.kind for r in res.raised_by] == ["subject-tag", "keyword", "keyword"]
    assert res.raised_above_request


def test_check_defaults_to_a_normal_request():
    assert privacy.check("s", "Quenby", R(kws=["quenby"])).visibility == "private"
    assert privacy.check("s", "plain", R()).visibility == "normal"


# ----------------------------------------------------------------- rules file

def test_absent_rules_file_means_empty_rules(tmp_path):
    rules = privacy.load_rules(str(tmp_path / "privacy_rules.json"))
    assert rules.subject_tags == {} and rules.keywords == ()


@pytest.mark.parametrize("content", [
    "{not json", "[]", '"str"', "null", "5", '{"keywords": "mom"}', '{"keywords": [1]}', '{"keywords": ["  "]}',
    '{"subject_tags": []}', '{"subject_tags": {"Bad Name": "private"}}', '{"subject_tags": {"x": "public"}}',
    '{"surprise": 1}', '{"version": 2}', '{"keywords": ["!!"]}', '{"keywords": ["a\\u0000b"]}'])
def test_corrupt_or_malformed_rules_raise_a_clear_error(tmp_path, content):
    path = tmp_path / "privacy_rules.json"
    path.write_text(content)
    with pytest.raises(privacy.PrivacyRulesError) as e:
        privacy.load_rules(str(path))
    assert str(e.value)
    assert path.read_text() == content  # never rewritten on error


def test_non_utf8_rules_file_is_a_clear_error(tmp_path):
    path = tmp_path / "privacy_rules.json"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(privacy.PrivacyRulesError):
        privacy.load_rules(str(path))


def test_edit_helpers_round_trip_through_save_and_load(tmp_path):
    path = str(tmp_path / "privacy_rules.json")
    rules, changed = privacy.tag_subject(Rules(), "family")
    assert changed
    assert privacy.tag_subject(rules, "family")[1] is False
    rules, _ = privacy.add_keyword(rules, "  Zorblax ")
    assert privacy.add_keyword(rules, "ZORBLAX")[1] is False
    privacy.save_rules(rules, path)
    loaded = privacy.load_rules(path)
    assert loaded.subject_tags == {"family": "private"} and loaded.keywords == ("zorblax",)
    loaded, c1 = privacy.untag_subject(loaded, "family")
    loaded, c2 = privacy.remove_keyword(loaded, "zorblax")
    assert c1 and c2
    assert privacy.untag_subject(loaded, "family")[1] is False
    assert privacy.remove_keyword(loaded, "zorblax")[1] is False


def test_edit_helpers_reject_bad_input():
    for call in (lambda: privacy.tag_subject(Rules(), "Not Kebab"), lambda: privacy.tag_subject(Rules(), "ok", "public"),
                 lambda: privacy.add_keyword(Rules(), ""), lambda: privacy.add_keyword(Rules(), None)):
        with pytest.raises(privacy.PrivacyRulesError):
            call()


def test_a_tag_on_a_nonexistent_subject_is_allowed_and_harmless(ingest):
    con = ingest.run11([F(subject="other", visibility="normal")])
    privacy.apply_rules_to_db(con, Rules(subject_tags={"not-yet": "private"}))  # no error
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "normal"


# ----------------------------------------------------------------- ingest (04 and 11) and rebuild

def write_rules(ingest, **kw):
    with open(os.path.join(ingest.env.data_dir, "privacy_rules.json"), "w") as f:
        json.dump(kw, f)


def vis(con):
    return {r["statement"]: r["visibility"] for r in con.execute("SELECT statement, visibility FROM facts")}


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_applies_tags_and_keywords_and_never_lowers(ingest, run):
    write_rules(ingest, subject_tags={"family": "private"}, keywords=["Zorblax"])
    con = getattr(ingest, run)([
        F(subject="family", statement="Plain.", visibility="normal"),
        F(subject="work", statement="Met Zorblax.", visibility="normal"),
        F(subject="work", statement="Neutral.", visibility="normal"),
        F(subject="work", statement="Kept private.", visibility="private"),
    ])
    assert vis(con) == {"Plain.": "private", "Met Zorblax.": "private", "Neutral.": "normal", "Kept private.": "private"}
    assert con.execute("SELECT private FROM subjects WHERE name='family'").fetchone()[0] == 1
    assert con.execute("SELECT private FROM subjects WHERE name='work'").fetchone()[0] == 0


def test_ingest_without_a_rules_file_is_unchanged(ingest):
    con = ingest.run11([F(statement="A.", visibility="normal"), F(statement="B.")])
    assert vis(con) == {"A.": "normal", "B.": "private"}


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_with_a_corrupt_rules_file_fails_loudly(ingest, run):
    with open(os.path.join(ingest.env.data_dir, "privacy_rules.json"), "w") as f:
        f.write("[1,2")
    with pytest.raises(privacy.PrivacyRulesError):
        getattr(ingest, run)([F(visibility="normal")])


def test_a_tag_on_a_parent_reaches_children_via_the_tree(ingest):
    write_rules(ingest, subject_tags={"family": "private"})
    con = ingest.run04([F(subject="cousins", statement="C.", visibility="normal")])
    con.execute("INSERT INTO subjects (name) VALUES ('family')")
    con.execute("UPDATE subjects SET parent_id = (SELECT id FROM subjects WHERE name='family') WHERE name='cousins'")
    ingest.run11([F(subject="other", statement="O.", visibility="normal")], con=con)  # last step sees the tree
    assert vis(con) == {"C.": "private", "O.": "normal"}
    assert con.execute("SELECT private FROM subjects WHERE name='cousins'").fetchone()[0] == 1


def test_changing_a_rule_and_rebuilding_re_privatizes_old_facts(ingest):
    items = [F(statement="Dinner with Quenby.", visibility="normal")]
    assert vis(ingest.run11(items)) == {"Dinner with Quenby.": "normal"}
    write_rules(ingest, keywords=["quenby"])
    assert vis(ingest.run11(items)) == {"Dinner with Quenby.": "private"}


def test_removing_a_rule_does_not_downgrade_a_fact_stored_private(ingest):
    write_rules(ingest, keywords=[])  # the rule that once caught it is gone
    items = [F(statement="Dinner with Quenby.", visibility="private"), F(statement="Open.", visibility="normal")]
    assert vis(ingest.run11(items)) == {"Dinner with Quenby.": "private", "Open.": "normal"}


def test_apply_rules_to_db_is_raise_only_and_idempotent(ingest):
    con = ingest.run11([F(statement="A Quenby.", visibility="normal"), F(statement="B.", visibility="private")])
    assert len(privacy.apply_rules_to_db(con, R(kws=["quenby"]))["raised"]) == 1
    assert privacy.apply_rules_to_db(con, R(kws=["quenby"]))["raised"] == []
    privacy.apply_rules_to_db(con, R())  # empty rules: nothing is lowered
    assert vis(con) == {"A Quenby.": "private", "B.": "private"}


def test_subject_private_column_is_constrained(ingest):
    con = ingest.db()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO subjects (name, private) VALUES ('x', 2)")
    con.execute("INSERT INTO subjects (name) VALUES ('y')")
    assert con.execute("SELECT private FROM subjects").fetchone()[0] == 0


# ----------------------------------------------------------------- add_fact

def _add(ingest, fact, **kw):
    import add_fact
    path = os.path.join(ingest.env.data_dir, "general_facts.json")
    kw.setdefault("db_path", os.path.join(ingest.env.root, "none.db"))
    return add_fact.append_fact(fact, data_path=path, **kw)


def _db_with(ingest, *subjects):
    db = os.path.join(ingest.env.root, "k.db")
    con = sqlite3.connect(db)
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    for name, parent in subjects:
        con.execute("INSERT INTO subjects (name, parent_id) VALUES (?, (SELECT id FROM subjects WHERE name = ?))", (name, parent))
    con.commit()
    con.close()
    return db


def test_add_fact_stores_the_resolved_visibility_and_explains_it(ingest):
    write_rules(ingest, subject_tags={"family": "private"}, keywords=["quenby"])
    import add_fact
    N = add_fact.NewFact
    a = _add(ingest, N(statement="Plain.", subject="family", trust_level="low", visibility="normal"))
    b = _add(ingest, N(statement="Saw Quenby.", subject="work", trust_level="low", visibility="normal"))
    c = _add(ingest, N(statement="Neutral.", subject="work", trust_level="low", visibility="normal"))
    assert [a.entry["visibility"], b.entry["visibility"], c.entry["visibility"]] == ["private", "private", "normal"]
    assert a.privacy.raised_by[0].kind == "subject-tag" and b.privacy.raised_by[0].kind == "keyword"
    assert [e["visibility"] for e in ingest.env.entries()] == ["private", "private", "normal"]


def test_add_fact_unknown_subject_is_private_when_a_db_exists(ingest):
    db = _db_with(ingest, ("known", None))
    import add_fact
    N = add_fact.NewFact
    a = _add(ingest, N(statement="x", subject="known", trust_level="low", visibility="normal"), db_path=db)
    b = _add(ingest, N(statement="y", subject="brand-new", trust_level="low", visibility="normal"), db_path=db)
    c = _add(ingest, N(statement="z", subject="brand-new", trust_level="low", visibility="normal"), db_path=db)
    assert a.entry["visibility"] == "normal"
    assert b.entry["visibility"] == "private" and b.privacy.raised_by[0].kind == "unknown-subject"
    assert c.entry["visibility"] == "normal"  # the subject is now in the facts file, so it is known


def test_add_fact_uses_the_db_tree_for_tag_inheritance(ingest):
    write_rules(ingest, subject_tags={"family": "private"})
    db = _db_with(ingest, ("family", None), ("cousins", "family"))
    import add_fact
    r = _add(ingest, add_fact.NewFact(statement="x", subject="cousins", trust_level="low", visibility="normal"), db_path=db)
    assert r.entry["visibility"] == "private"


def test_add_fact_with_a_corrupt_rules_file_refuses_and_writes_nothing(ingest):
    with open(os.path.join(ingest.env.data_dir, "privacy_rules.json"), "w") as f:
        f.write("{oops")
    import add_fact
    r = _add(ingest, add_fact.NewFact(statement="x", subject="s", trust_level="low", visibility="normal"))
    assert not r.ok and "privacy_rules.json" in r.errors[0]
    assert not os.path.exists(os.path.join(ingest.env.data_dir, "general_facts.json"))


def test_add_fact_without_rules_keeps_todays_behavior(ingest):
    import add_fact
    r = _add(ingest, add_fact.NewFact(statement="x", subject="s", trust_level="low", visibility="normal"))
    assert r.ok and r.entry["visibility"] == "normal" and r.privacy.visibility == "normal"


def test_add_fact_cli_applies_the_rules(ingest):
    write_rules(ingest, keywords=["quenby"])
    r = ingest.env.cli("Saw Quenby.", "--subject", "work", "--trust", "low", "--visibility", "normal", "--allow-dirty")
    assert r.returncode == 0, r.stderr
    assert ingest.env.entries()[0]["visibility"] == "private"


def test_add_then_remove_rule_then_rebuild_keeps_the_fact_private(ingest):
    write_rules(ingest, keywords=["quenby"])
    import add_fact
    _add(ingest, add_fact.NewFact(statement="Saw Quenby.", subject="work", trust_level="low", visibility="normal"))
    write_rules(ingest, keywords=[])
    path = os.path.join(ingest.env.data_dir, "general_facts.json")
    assert vis(ingest.run11(json.load(open(path)))) == {"Saw Quenby.": "private"}


# ----------------------------------------------------------------- repo hygiene and the "cannot be ignored" scans

def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [f for f in out.splitlines() if f]


def test_no_rules_file_and_no_synthetic_names_outside_the_tests():
    files = _tracked_files()
    assert not [f for f in files if os.path.basename(f) == "privacy_rules.json"]
    leaks = []
    for f in files:
        if f.startswith("tests/") or not os.path.isfile(os.path.join(REPO, f)):
            continue
        try:
            text = open(os.path.join(REPO, f), encoding="utf-8").read().lower()
        except (UnicodeDecodeError, OSError):
            continue
        leaks += [f"{f}: {n}" for n in SYNTHETIC_NAMES if n.lower() in text]
    assert leaks == []


def test_rules_file_never_appears_in_git_history():
    r = subprocess.run(["git", "log", "--all", "--oneline", "--", "*privacy_rules.json"], cwd=REPO, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("not a git checkout")
    assert r.stdout.strip() == ""


def test_every_module_that_writes_facts_goes_through_the_privacy_rules():
    """04, 11 and add_fact all set facts.visibility. A new writer that skips the resolver would
    silently store an unchecked value, so any non-test module inserting into `facts` or building a
    fact entry must reference `privacy`."""
    offenders = []
    for name in sorted(os.listdir(REPO)):
        if not name.endswith(".py") or name == "privacy.py":
            continue
        src = open(os.path.join(REPO, name)).read()
        if ("INSERT INTO facts" in src or "def build_entry" in src) and "privacy." not in src:
            offenders.append(name)
    assert offenders == []


def test_add_fact_passes_the_resolved_value_to_build_entry():
    tree = ast.parse(open(os.path.join(REPO, "add_fact.py")).read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "append_fact")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "build_entry"]
    assert calls and all(any(k.arg == "visibility" for k in c.keywords) for c in calls)
