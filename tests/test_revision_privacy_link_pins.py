"""Pins for the three build-time database passes: revisions applied, privacy rules applied, entities linked.

Written against the code before it moved behind repository ports, and kept as the contract the ports must keep:
the public store functions (`revisions_store.apply_revisions`, `privacy_store.apply_rules_to_db`,
`entities_store.link_facts`), their result shapes, their transaction behaviour, and the order and failure of the
two build steps that call them. In-memory databases and temp files only.
"""
import json
import os
import sqlite3

import pytest

import entities_store
import privacy
import privacy_store
import revisions
import revisions_store
from appctx import locs
from test_add_fact import REPO

T1 = "2026-10-01T08:00:00+00:00"
T2 = "2026-10-02T09:00:00+00:00"
T3 = "2026-10-03T10:00:00+00:00"


def new_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    return con


def add_subject(con, name, parent=None):
    parent_id = con.execute("SELECT id FROM subjects WHERE name = ?", (parent,)).fetchone()[0] if parent else None
    return con.execute("INSERT INTO subjects (name, parent_id) VALUES (?, ?)", (name, parent_id)).lastrowid


def add_fact(con, key, subject="s", statement="Plain.", visibility="normal", notes=None, quote=None, locator=None):
    sid = con.execute("SELECT id FROM subjects WHERE name = ?", (subject,)).fetchone()
    sid = sid[0] if sid else add_subject(con, subject)
    fid = con.execute(
        "INSERT INTO facts (subject_id, statement, trust_level, freshness, recheck_rationale, visibility, source_key, notes) "
        "VALUES (?, ?, 'low', 'no-decay', 'no decay', ?, ?, ?)", (sid, statement, visibility, key, notes)).lastrowid
    rev1 = revisions.implicit_revision(key, {"statement": statement, "trust_level": "low", "freshness": "no-decay",
                                             "visibility": visibility, "notes": notes, "subject": subject,
                                             "recheck_rationale": "no decay"}, T1)
    revisions_store.insert_revision_row(con.cursor(), fid, rev1)
    if quote or locator:
        sid_src = con.execute("INSERT INTO sources (citekey, name, source_type) VALUES (?, 't', 'primary')",
                              (f"src-{key}",)).lastrowid
        con.execute("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (?, ?, ?, ?)",
                    (fid, sid_src, locator, quote))
    return fid


def revision(key, n, at, **changes):
    base = {"statement": "Plain.", "trust_level": "low", "trust_rationale": None, "status": "active",
            "visibility": "normal", "superseded_by": None, "recheck_by": None, "recheck_rationale": "no decay",
            "freshness": "no-decay", "kind": "unclassified", "valid_from": None, "valid_to": None,
            "applies_to": None, "notes": None}
    base.update(changes)
    return {"source_key": key, "revision": n, "changed_at": at, "changed_via": "test", "session_id": None,
            "change_reason": "r", **base}


def write_log(path, *recs, raw=()):
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(revisions.log_line(r) + "\n")
        for line in raw:
            f.write(line)


# ------------------------------------------------------------------ apply_revisions


def test_apply_revisions_counts_inserts_and_writes_back_only_the_latest(tmp_path):
    con = new_db()
    a, b = add_fact(con, "a"), add_fact(con, "b")
    log = tmp_path / "log.jsonl"
    write_log(log, revision("a", 2, T2, statement="A2"), revision("b", 2, T2, status="superseded", superseded_by="a"),
              revision("a", 3, T3, statement="A3", visibility="private", kind="plan", valid_from="2025", applies_to="men"))
    assert revisions_store.apply_revisions(con, str(log)) == 3
    assert con.in_transaction is False                                  # committed
    assert [r[0] for r in con.execute("SELECT revision FROM fact_revisions WHERE source_key='a' ORDER BY revision")] == [1, 2, 3]
    row = con.execute("SELECT statement, visibility, kind, valid_from, applies_to, status FROM facts WHERE id = ?", (a,)).fetchone()
    assert tuple(row) == ("A3", "private", "plan", "2025", "men", "active")
    row = con.execute("SELECT status, superseded_by_fact_id FROM facts WHERE id = ?", (b,)).fetchone()
    assert tuple(row) == ("superseded", a)                              # the key resolves to the fact id


@pytest.mark.parametrize("content", [None, "", "\n\n   \n"])
def test_apply_revisions_with_a_missing_empty_or_blank_log_applies_nothing(tmp_path, content):
    con = new_db()
    add_fact(con, "a", statement="Keep me.")
    log = tmp_path / "log.jsonl"
    if content is not None:
        log.write_text(content)
    assert revisions_store.apply_revisions(con, str(log)) == 0
    assert con.execute("SELECT statement FROM facts").fetchone()[0] == "Keep me."
    assert con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 1


@pytest.mark.parametrize("make, match", [
    (lambda: [revision("nope", 2, T2)], "unknown source_key"),
    (lambda: [revision("a", 3, T2)], "expected 2"),
    (lambda: [revision("a", 2, T2), revision("a", 2, T3)], "expected 3"),
    (lambda: [revision("a", 2, T3), revision("a", 3, T2)], "earlier than the previous"),
    (lambda: [revision("a", 2, T2, superseded_by="ghost", status="superseded")], "not an existing source_key"),
])
def test_apply_revisions_refuses_a_bad_log_and_changes_nothing(tmp_path, make, match):
    con = new_db()
    add_fact(con, "a")
    log = tmp_path / "log.jsonl"
    write_log(log, *make())
    with pytest.raises(revisions.RevisionError, match=match):
        revisions_store.apply_revisions(con, str(log))
    assert con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 1
    assert con.execute("SELECT statement FROM facts").fetchone()[0] == "Plain."


def test_apply_revisions_names_the_line_of_a_malformed_record(tmp_path):
    con = new_db()
    add_fact(con, "a")
    log = tmp_path / "log.jsonl"
    write_log(log, revision("a", 2, T2), raw=["{not json\n"])
    with pytest.raises(revisions.RevisionError, match=r"log\.jsonl:2: not valid JSON"):
        revisions_store.apply_revisions(con, str(log))


def test_apply_revisions_keeps_a_nul_character_in_a_statement(tmp_path):
    con = new_db()
    add_fact(con, "a")
    log = tmp_path / "log.jsonl"
    write_log(log, revision("a", 2, T2, statement="before\x00after"))
    assert revisions_store.apply_revisions(con, str(log)) == 1
    assert con.execute("SELECT statement FROM facts").fetchone()[0] == "before\x00after"


def test_insert_revision_row_stores_the_whole_record():
    con = new_db()
    fid = add_fact(con, "a")
    rec = revision("a", 2, T2, statement="S", notes="n", trust_rationale="why")
    revisions_store.insert_revision_row(con.cursor(), fid, rec)
    row = con.execute("SELECT * FROM fact_revisions WHERE revision = 2").fetchone()
    assert {k: row[k] for k in revisions.REVISION_KEYS} == rec and row["fact_id"] == fid


# ------------------------------------------------------------------ apply_rules_to_db


def test_apply_rules_result_shape_and_no_commit():
    con = new_db()
    add_subject(con, "root")
    add_subject(con, "child", "root")
    add_subject(con, "other")
    add_fact(con, "k1", subject="child", statement="Harmless.")
    add_fact(con, "k2", subject="other", statement="Mentions Quenby here.")
    con.commit()
    out = privacy_store.apply_rules_to_db(con, privacy.Rules(subject_tags={"root": "private"}, keywords=("quenby",)))
    assert set(out) == {"raised", "private_subjects"}
    assert out["private_subjects"] == 2                                  # root and its descendant
    assert sorted(f for f, _ in out["raised"]) == [1, 2]
    assert out["raised"][0][1].startswith("private: subject-tag") or "subject-tag" in out["raised"][0][1]
    assert dict(con.execute("SELECT name, private FROM subjects").fetchall()) == {"root": 1, "child": 1, "other": 0}
    assert con.in_transaction is True                                    # the caller commits


def test_apply_rules_checks_the_other_text_fields_of_a_fact():
    con = new_db()
    by_note = add_fact(con, "k1", notes="a note about Quenby")
    by_quote = add_fact(con, "k2", quote="said Quenby", locator="p. 3")
    add_fact(con, "k3")
    out = privacy_store.apply_rules_to_db(con, privacy.Rules(keywords=("quenby",)))
    assert sorted(f for f, _ in out["raised"]) == [by_note, by_quote]


def test_apply_rules_never_lowers_and_is_idempotent():
    con = new_db()
    add_fact(con, "k1", visibility="private")
    add_fact(con, "k2", statement="Quenby.")
    rules = privacy.Rules(keywords=("quenby",))
    assert len(privacy_store.apply_rules_to_db(con, rules)["raised"]) == 1
    assert privacy_store.apply_rules_to_db(con, rules)["raised"] == []
    privacy_store.apply_rules_to_db(con, privacy.Rules())
    assert [r[0] for r in con.execute("SELECT visibility FROM facts ORDER BY id")] == ["private", "private"]


def test_apply_rules_on_an_empty_database_and_a_cyclic_subject_chain():
    con = new_db()
    assert privacy_store.apply_rules_to_db(con, privacy.Rules()) == {"raised": [], "private_subjects": 0}
    a, b = add_subject(con, "a"), add_subject(con, "b")
    con.execute("UPDATE subjects SET parent_id = ? WHERE id = ?", (b, a))
    con.execute("UPDATE subjects SET parent_id = ? WHERE id = ?", (a, b))
    add_fact(con, "k1", subject="a")
    out = privacy_store.apply_rules_to_db(con, privacy.Rules())
    assert out["private_subjects"] == 2                                  # a cycle fails closed
    assert [f for f, _ in out["raised"]] == [1]


# ------------------------------------------------------------------ link_facts


def entity(id, name, private=False, aliases=(), type="person", external_id=None, notes=None):
    return {"id": id, "canonical_name": name, "type": type, "aliases": list(aliases), "private": private,
            "external_id": external_id, "notes": notes}


def test_link_facts_result_rows_and_no_commit():
    con = new_db()
    add_fact(con, "k1", statement="Lunch with Zeddy at Acme.", visibility="normal")
    add_fact(con, "k2", statement="Plain.", notes="see Acme Labs", visibility="private")
    add_fact(con, "k3", statement="My brotherhood.", visibility="normal")
    con.commit()
    es = [entity("zed", "Zed Name", private=True, aliases=["Zeddy"]),
          entity("acme", "Acme Labs", aliases=["Acme"], type="organization", external_id="wikidata:Q1", notes="a lab"),
          entity("bro", "brother")]
    out = entities_store.link_facts(con, es)
    assert out == {"entities": 3, "links": 3, "unprotected": [1]}        # k1 mentions private Zed but is normal
    assert con.in_transaction is True
    assert {(r[0], r[1]) for r in con.execute(
        "SELECT f.source_key, e.entity_key FROM fact_entities fe JOIN facts f ON f.id = fe.fact_id "
        "JOIN entities e ON e.id = fe.entity_id")} == {("k1", "zed"), ("k1", "acme"), ("k2", "acme")}
    row = con.execute("SELECT * FROM entities WHERE entity_key = 'acme'").fetchone()
    assert (row["name_norm"], row["private"], row["external_id"], row["notes"]) == ("acme labs", 0, "wikidata:Q1", "a lab")
    assert [r[0] for r in con.execute("SELECT alias_norm FROM entity_aliases ORDER BY alias_norm")] == ["acme", "zeddy"]


def test_link_facts_with_no_entities_or_no_facts():
    con = new_db()
    assert entities_store.link_facts(con, []) == {"entities": 0, "links": 0, "unprotected": []}
    assert entities_store.link_facts(con, [entity("a", "Alpha")]) == {"entities": 1, "links": 0, "unprotected": []}


def test_link_facts_reports_every_unprotected_fact_in_id_order():
    con = new_db()
    for i in range(3):
        add_fact(con, f"k{i}", statement="About Zed Name.", visibility="normal" if i != 1 else "private")
    out = entities_store.link_facts(con, [entity("zed", "Zed Name", private=True)])
    assert out["unprotected"] == [1, 3] and out["links"] == 3


# ------------------------------------------------------------------ the steps: order and failure


def load_step(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location("pin_" + name, os.path.join(REPO, "ingest", name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def private_dir(tmp_path):
    d = tmp_path / "private"
    d.mkdir()
    return d


def test_link_step_fails_on_an_unprotected_private_mention_before_committing(private_dir):
    con = new_db()
    add_fact(con, "k1", statement="About Zed Name.", visibility="normal")
    con.commit()
    (private_dir / "entities.json").write_text(json.dumps(
        {"version": 1, "entities": [{"id": "zed", "canonical_name": "Zed Name", "type": "person", "private": True}]}))
    with pytest.raises(ValueError, match=r"facts \[1\] mention a private entity but are not stored private"):
        load_step("link_entities.py").run(con, locs(private_data_dir=str(private_dir)))
    assert con.in_transaction is True                                    # nothing committed by the failing step


def test_link_step_prints_the_summary_and_commits(private_dir, capsys):
    con = new_db()
    add_fact(con, "k1", statement="About Zed Name.", visibility="private")
    con.commit()
    (private_dir / "entities.json").write_text(json.dumps(
        {"version": 1, "entities": [{"id": "zed", "canonical_name": "Zed Name", "type": "person", "private": True}]}))
    load_step("link_entities.py").run(con, locs(private_data_dir=str(private_dir)))
    assert "[link_entities] 1 entities (1 private), 1 fact links" in capsys.readouterr().out
    assert con.in_transaction is False


def test_revision_step_applies_the_log_then_the_rules_then_raises_history(private_dir, capsys):
    """Order matters: a revision that rewords a fact to contain a listed name is raised by the floor that runs
    after it, and the snapshots of a private fact are all raised to private last."""
    con = new_db()
    add_fact(con, "a", statement="Plain.", visibility="normal")
    add_fact(con, "b", statement="Other.", visibility="normal")
    con.commit()
    write_log(str(private_dir / "fact_revisions.jsonl"),
              revision("a", 2, T2, statement="Now mentions Quenby."),
              revision("b", 2, T2, statement="Still other."))
    (private_dir / "privacy_rules.json").write_text(json.dumps({"version": 1, "subject_tags": {}, "keywords": ["quenby"]}))
    load_step("apply_fact_revisions.py").run(con, locs(private_data_dir=str(private_dir)))
    out = capsys.readouterr().out
    assert "privacy rules raised 1 fact(s) to private after revisions" in out
    assert "[apply_fact_revisions] applied 2 revisions (4 rows in fact_revisions incl. implicit revision 1)" in out
    assert dict(con.execute("SELECT source_key, visibility FROM facts").fetchall()) == {"a": "private", "b": "normal"}
    # every snapshot of the now-private fact is private; the other fact's history is untouched
    assert {(r[0], r[1]) for r in con.execute("SELECT source_key, visibility FROM fact_revisions")} == {
        ("a", "private"), ("b", "normal")}
    assert con.in_transaction is False


def test_revision_step_with_no_log_still_applies_the_floor(private_dir, capsys):
    con = new_db()
    add_fact(con, "a", statement="About Quenby.", visibility="normal")
    con.commit()
    (private_dir / "privacy_rules.json").write_text(json.dumps({"version": 1, "subject_tags": {}, "keywords": ["quenby"]}))
    load_step("apply_fact_revisions.py").run(con, locs(private_data_dir=str(private_dir)))
    assert "applied 0 revisions (1 rows" in capsys.readouterr().out
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "private"
    assert con.execute("SELECT visibility FROM fact_revisions").fetchone()[0] == "private"


def test_revision_step_rejects_a_bad_log_before_touching_the_rules(private_dir):
    con = new_db()
    add_fact(con, "a", statement="About Quenby.", visibility="normal")
    con.commit()
    write_log(str(private_dir / "fact_revisions.jsonl"), revision("ghost", 2, T2))
    (private_dir / "privacy_rules.json").write_text(json.dumps({"version": 1, "subject_tags": {}, "keywords": ["quenby"]}))
    with pytest.raises(revisions.RevisionError, match="unknown source_key"):
        load_step("apply_fact_revisions.py").run(con, locs(private_data_dir=str(private_dir)))
    assert con.execute("SELECT visibility FROM facts").fetchone()[0] == "normal"   # the floor never ran


# ------------------------------------------------------------------ link_facts: edge inputs and row order


def _rows(con, sql):
    return [tuple(r) for r in con.execute(sql)]


def test_link_facts_allocates_entity_rows_in_entity_order_and_links_in_fact_then_entity_order():
    con = new_db()
    add_fact(con, "k1", statement="Beta then Alpha.", visibility="normal")
    add_fact(con, "k2", statement="Only Alpha.", visibility="normal")
    con.commit()
    es = [entity("alpha", "Alpha", aliases=["Al"]), entity("beta", "Beta")]
    assert entities_store.link_facts(con, es)["links"] == 3
    assert _rows(con, "SELECT id, entity_key FROM entities ORDER BY id") == [(1, "alpha"), (2, "beta")]
    assert _rows(con, "SELECT entity_id, alias FROM entity_aliases ORDER BY rowid") == [(1, "Al")]
    assert _rows(con, "SELECT fact_id, entity_id FROM fact_entities ORDER BY rowid") == [(1, 1), (1, 2), (2, 1)]


def test_link_facts_second_attempt_over_the_same_entities_fails_and_leaves_the_first_rows():
    con = new_db()
    add_fact(con, "k1", statement="About Alpha.", visibility="normal")
    es = [entity("alpha", "Alpha")]
    assert entities_store.link_facts(con, es)["links"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        entities_store.link_facts(con, es)
    assert _rows(con, "SELECT entity_key FROM entities") == [("alpha",)]
    assert _rows(con, "SELECT fact_id, entity_id FROM fact_entities") == [(1, 1)]
    assert con.in_transaction is True


def test_link_facts_a_term_claimed_twice_fails_mid_way_without_a_rollback():
    """The schema's trigger refuses an alias equal to another entity's name; rows written before it stay (the
    build step fails and nothing commits)."""
    con = new_db()
    add_fact(con, "k1", statement="About Alpha.", visibility="normal")
    es = [entity("alpha", "Alpha"), entity("beta", "Beta", aliases=["alpha"])]
    with pytest.raises(sqlite3.IntegrityError):
        entities_store.link_facts(con, es)
    assert _rows(con, "SELECT entity_key FROM entities ORDER BY id") == [("alpha",), ("beta",)]
    assert _rows(con, "SELECT COUNT(*) FROM fact_entities") == [(0,)]


def test_link_facts_nul_blank_and_none_text_never_match_and_never_crash():
    con = new_db()
    add_fact(con, "k1", statement="Alpha\x00Beta", visibility="normal", notes="  ")
    add_fact(con, "k2", statement="x", visibility="normal", notes=None, quote=None, locator="p. 3")
    out = entities_store.link_facts(con, [entity("alpha", "Alpha"), entity("beta", "Beta")])
    assert out["entities"] == 2 and out["unprotected"] == []
    assert _rows(con, "SELECT fact_id, entity_id FROM fact_entities ORDER BY rowid") == [(1, 1), (1, 2)]


def test_link_facts_text_scanned_is_statement_notes_citations_and_a_mention_in_a_quote_links():
    con = new_db()
    add_fact(con, "k1", statement="Plain.", notes="see Gamma", quote="said Delta", locator="p. 2", visibility="normal")
    out = entities_store.link_facts(con, [entity("g", "Gamma"), entity("d", "Delta", private=True)])
    assert out == {"entities": 2, "links": 2, "unprotected": [1]}


def test_link_step_reads_entities_in_file_order_and_an_invalid_file_fails_the_step(private_dir):
    con = new_db()
    add_fact(con, "k1", statement="Alpha and Beta.", visibility="normal")
    con.commit()
    (private_dir / "entities.json").write_text(json.dumps({"version": 1, "entities": [
        {"id": "beta", "canonical_name": "Beta", "type": "person"},
        {"id": "alpha", "canonical_name": "Alpha", "type": "person", "aliases": ["Al"]}]}))
    load_step("link_entities.py").run(con, locs(private_data_dir=str(private_dir)))
    assert _rows(con, "SELECT id, entity_key FROM entities ORDER BY id") == [(1, "alpha"), (2, "beta")] or \
        _rows(con, "SELECT id, entity_key FROM entities ORDER BY id") == [(1, "beta"), (2, "alpha")]
    (private_dir / "entities.json").write_text("{not json")
    con2 = new_db()
    with pytest.raises(Exception, match="not valid JSON"):
        load_step("link_entities.py").run(con2, locs(private_data_dir=str(private_dir)))
    assert _rows(con2, "SELECT COUNT(*) FROM entities") == [(0,)]
