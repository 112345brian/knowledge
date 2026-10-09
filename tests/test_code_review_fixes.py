"""Regression tests for the findings of the /code-review pass (2026-10-04).

1. review.current_states() raised on the REAL legacy entries (no `freshness` key, in pilot_facts.json /
   facts_batch*.json) because it did not pass the entry's file to implicit_revision: so approve, reject,
   the inbox page and every lifecycle command failed on real data while fixture tests (which always
   carry `freshness`) passed.
2. The privacy keyword rules only ever scanned a fact's `statement`; notes, rationales, quotes and
   citation text are stored, searchable and (some of them) copied into the remotely served tier.
Synthetic names only.
"""
import json
import os
import sqlite3
import sys
import types

import pytest

from test_fact_revisions import world, T1, T2  # noqa: F401  (fixture import)

NAME = "Quenbyx"  # synthetic; the keyword the rules list


def legacy(key="legacy-aaaaaaaaaa", **kw):
    """An entry exactly as the real legacy files hold them: no `freshness`, no `captured_via`."""
    e = {"source_key": key, "subject": "alpha", "statement": "A harmless statement.", "trust_level": "low",
         "is_original_claim": False, "is_personal": True, "date_added": T1}
    e.update(kw)
    return e


def new_style(key="k1", **kw):
    e = {"source_key": key, "subject": "alpha", "statement": "A harmless statement.", "trust_level": "low",
         "is_original_claim": False, "is_personal": True, "date_added": T1, "visibility": "normal",
         "freshness": "no-decay", "recheck_rationale": "does not decay"}
    e.update(kw)
    return e


def write_rules(world, tags=None, keywords=(NAME.lower(),)):
    with open(os.path.join(world.env.data_dir, "privacy_rules.json"), "w") as f:
        json.dump({"version": 1, "subject_tags": tags if tags is not None else {"alpha": "normal"},
                   "keywords": list(keywords)}, f)


@pytest.fixture
def mods(world):
    names = ("privacy", "privacy_store", "review", "review_service", "review_store", "review_rules", "lifecycle", "lifecycle_service", "lifecycle_rules", "lifecycle_store", "normal_db")
    saved = {m: sys.modules.pop(m) for m in names if m in sys.modules}
    import privacy, privacy_store, review, lifecycle, normal_db
    yield types.SimpleNamespace(privacy=privacy, privacy_store=privacy_store, review=review, lifecycle=lifecycle, normal_db=normal_db)
    # put the originals back: other tests in this worker hold them (e.g. PrivacyRulesError identity)
    for m in names:
        sys.modules.pop(m, None)
    sys.modules.update(saved)


def full_db_file(world, tmp_path):
    """The built full db as a file (normal_db reads a path)."""
    con = world.build()
    path = str(tmp_path / "full.db")
    dst = sqlite3.connect(path)
    con.backup(dst)
    dst.close()
    return path


# ------------------------------------------------------------------ 1. review.current_states on legacy data

def test_current_states_works_on_legacy_entries_without_freshness(world, mods):
    world.seed(pilot=[legacy("legacy-aaaaaaaaaa"),
                      legacy("legacy-bbbbbbbbbb", statement="Has a date.", recheck_by="2027-01-01")])
    states = mods.review.current_states(world.env.data_dir)
    assert states["legacy-aaaaaaaaaa"]["freshness"] == "unreviewed"     # no recheck_by
    assert states["legacy-bbbbbbbbbb"]["freshness"] == "recheck"        # has one
    assert {s["status"] for s in states.values()} == {"active"}


def test_retract_works_on_a_legacy_fact_and_the_build_shows_it(world, mods):
    world.seed(pilot=[legacy("legacy-aaaaaaaaaa")])
    res = mods.lifecycle.retract("legacy-aaaaaaaaaa", "was wrong", data_dir=world.env.data_dir, commit=False)
    assert res.ok and res.outcome == "retracted", (res.outcome, res.reason, res.errors)
    con = world.build()
    assert con.execute("SELECT status, freshness FROM facts").fetchone()[:] == ("retracted", "unreviewed")


def test_approve_on_a_legacy_fact_is_a_clean_skip_not_a_crash(world, mods):
    world.seed(pilot=[legacy("legacy-aaaaaaaaaa")])
    res = mods.review.approve(["legacy-aaaaaaaaaa"], data_dir=world.env.data_dir, commit=False)
    assert not res.errors
    assert [i.outcome for i in res.items] == ["skipped"]


def test_edit_works_on_a_legacy_fact(world, mods):
    world.seed(pilot=[legacy("legacy-aaaaaaaaaa")])
    res = mods.lifecycle.edit_fact("legacy-aaaaaaaaaa", "clearer", statement="A clearer statement.",
                                   data_dir=world.env.data_dir, commit=False)
    assert res.ok, (res.message, res.errors)


# ------------------------------------------------------------------ 2. keyword rules see every text field

def test_resolver_scans_extra_text_and_names_the_field(mods):
    p = mods.privacy
    rules = p.Rules(keywords=(NAME.lower(),))
    clean = p.resolve_visibility("alpha", "harmless", "normal", rules, extra_text=("nothing here", None, ""))
    assert clean.visibility == "normal"
    hit = p.resolve_visibility("alpha", "harmless", "normal", rules, extra_text=(None, f"{NAME} told me",))
    assert hit.visibility == "private"
    assert [r.kind for r in hit.raised_by] == ["keyword"] and "another field" in hit.explain()
    both = p.resolve_visibility("alpha", f"{NAME} here", "normal", rules, extra_text=(f"{NAME} again",))
    assert [r.kind for r in both.raised_by] == ["keyword"]  # reported once, as the statement hit


def test_resolver_without_extra_text_is_unchanged(mods):
    p = mods.privacy
    rules = p.Rules(keywords=(NAME.lower(),))
    assert p.resolve_visibility("alpha", f"{NAME} here", "normal", rules).explain().count("statement") == 1


@pytest.mark.parametrize("field", ["notes", "trust_rationale", "recheck_rationale", "source_quote", "source_locator"])
def test_add_fact_raises_to_private_for_a_listed_name_in_any_text_field(world, field):
    write_rules(world)
    af = world.af
    kw = {field: f"{NAME} mentioned this"}
    if field in ("source_locator", "source_quote"):
        kw["source_citekey"] = "ck"  # a locator or quote needs a citekey (no db here: the check is skipped)
    fact = af.NewFact(statement="A harmless statement.", subject="alpha", trust_level="low", no_decay=True,
                      recheck_rationale=kw.pop("recheck_rationale", "does not decay"), visibility="normal", **kw)
    res = af.append_fact(fact, data_path=os.path.join(world.env.data_dir, "general_facts.json"),
                         db_path=os.path.join(world.env.db_dir, "none.db"))
    assert res.ok, res.errors
    assert res.entry["visibility"] == "private" and "another field" in res.privacy.explain()


def test_add_fact_stays_normal_without_a_listed_name(world):
    write_rules(world)
    af = world.af
    fact = af.NewFact(statement="A harmless statement.", subject="alpha", trust_level="low", no_decay=True,
                      recheck_rationale="does not decay", notes="nothing sensitive", visibility="normal")
    res = af.append_fact(fact, data_path=os.path.join(world.env.data_dir, "general_facts.json"),
                         db_path=os.path.join(world.env.db_dir, "none.db"))
    assert res.ok and res.entry["visibility"] == "normal"


@pytest.mark.parametrize("field", ["notes", "trust_rationale", "recheck_rationale"])
def test_rebuild_privatizes_a_normal_fact_with_a_listed_name_in_a_text_field(world, field):
    write_rules(world)
    world.seed(general=[new_style("k1", **{field: f"{NAME} said so"})])
    con = world.build()
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "private"


def test_rebuild_checks_citation_text_too(world):
    write_rules(world)
    world.seed(general=[new_style("k1")])
    con = world.build()
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "normal"
    con.execute("INSERT INTO sources (id, name, source_type) VALUES (1, 'A paper', 'primary')")
    con.execute("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (1, 1, 'p. 3', ?)",
                (f"{NAME} wrote this",))
    import privacy_store
    applied = privacy_store.apply_rules_to_db(con, privacy_store.load_rules(os.path.join(world.env.data_dir, "privacy_rules.json")))
    assert [fid for fid, _ in applied["raised"]] == [1]
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "private"


@pytest.mark.parametrize("where", ["trust_rationale", "recheck_rationale", "citation"])
def test_normal_db_leaves_out_a_fact_whose_copied_text_has_a_listed_name(world, mods, tmp_path, where):
    # Built WITHOUT rules (so the full db has the fact as normal), then the normal db is built with
    # rules added afterwards: the rule must still keep the fact out, whichever copied column holds it.
    kw = {"trust_rationale": f"{NAME} vouched"} if where == "trust_rationale" else \
         {"recheck_rationale": f"{NAME} will check"} if where == "recheck_rationale" else {}
    world.seed(general=[new_style("k1", **kw), new_style("k2", statement="Another harmless one.")])
    con = world.build()
    if where == "citation":
        con.execute("INSERT INTO sources (id, name, source_type) VALUES (1, 'A paper', 'primary')")
        con.execute("INSERT INTO fact_sources (fact_id, source_id, quote) VALUES (1, 1, ?)", (f"{NAME} wrote this",))
        con.commit()
    full = str(tmp_path / "full.db")
    dst = sqlite3.connect(full)
    con.backup(dst)
    dst.close()
    rules = mods.privacy.Rules(keywords=(NAME.lower(),))
    out = str(tmp_path / "normal.db")
    mods.normal_db.build_normal_db(full, out, rules)
    n = sqlite3.connect(out)
    assert [r[0] for r in n.execute("SELECT source_key FROM facts ORDER BY id")] == ["k2"]
    assert NAME.encode() not in open(out, "rb").read()


def test_normal_db_leaves_out_a_revision_with_a_listed_name_in_its_rationale(world, mods, tmp_path):
    world.seed(general=[new_style("k1")])
    assert world.append("k1", {"trust_rationale": f"{NAME} vouched", "trust_level": "high"}, "checked", at=T2).ok
    full = full_db_file(world, tmp_path)
    rules = mods.privacy.Rules(keywords=(NAME.lower(),))
    out = str(tmp_path / "normal.db")
    mods.normal_db.build_normal_db(full, out, rules)
    n = sqlite3.connect(out)
    # the current fact's rationale carries the name, so the fact (and with it all its history) is out
    assert n.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert n.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 0
    assert NAME.encode() not in open(out, "rb").read()


@pytest.mark.parametrize("field,value", [("notes", f"{NAME} knows"), ("trust_rationale", f"{NAME} vouched")])
def test_editing_a_text_field_to_add_a_listed_name_raises_visibility(world, mods, field, value):
    write_rules(world)
    world.seed(general=[new_style("k1")])
    res = mods.lifecycle.edit_fact("k1", "added detail", data_dir=world.env.data_dir, commit=False, **{field: value})
    assert res.ok, (res.message, res.errors)
    assert res.revision["visibility"] == "private"
    assert any("another field" in n for n in res.notes), res.notes


def test_editing_a_text_field_without_a_listed_name_keeps_visibility(world, mods):
    write_rules(world)
    world.seed(general=[new_style("k1")])
    res = mods.lifecycle.edit_fact("k1", "added detail", data_dir=world.env.data_dir, commit=False,
                                   notes="nothing sensitive")
    assert res.ok and res.revision["visibility"] == "normal"


# ------------------------------------------------------------------ 3. no process-wide umask toggling

def _current_umask():
    u = os.umask(0)
    os.umask(u)
    return u


def test_atomic_writers_never_touch_the_process_umask_and_new_files_follow_it(world, mods, monkeypatch, tmp_path):
    expected = 0o666 & ~_current_umask()

    def boom(_mask):
        raise AssertionError("os.umask was called: it changes the umask for every thread in the process")

    monkeypatch.setattr(os, "umask", boom)
    # add_fact: first fact creates general_facts.json
    af = world.af
    fact = af.NewFact(statement="A fact.", subject="alpha", trust_level="low", no_decay=True,
                      recheck_rationale="does not decay", visibility="private")
    facts_path = os.path.join(world.env.data_dir, "general_facts.json")
    assert af.append_fact(fact, data_path=facts_path, db_path=os.path.join(world.env.db_dir, "none.db")).ok
    # revisions: first revision creates fact_revisions.jsonl
    key = json.load(open(facts_path))[0]["source_key"]
    assert world.append(key, {"trust_level": "high"}, "checked").ok  # real clock: add_fact stamped today's date
    # privacy rules: save_rules creates privacy_rules.json
    rules_path = str(tmp_path / "privacy_rules.json")
    mods.privacy_store.save_rules(mods.privacy.Rules(keywords=(NAME.lower(),)), rules_path)
    for p in (facts_path, world.log, rules_path):
        assert os.stat(p).st_mode & 0o777 == expected, (p, oct(os.stat(p).st_mode))
    # and no temp litter
    assert not [n for n in os.listdir(world.env.data_dir) if n.endswith(".tmp")]
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
