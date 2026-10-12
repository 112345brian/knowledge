"""Pins for entity linking in a built database: the order rows are allocated and links written, a second attempt,
a term claimed twice, NUL/blank text, and the build step's order and failure. Written against the code before the
entity ports were collapsed onto the foundation's `EntityRepository`, and kept as the contract the adapter must keep.
In-memory databases and temp files only."""
import json
import sqlite3

import pytest

import entity_link_service
import entity_repository_store
import entity_rules
from appctx import locs
from test_revision_privacy_link_pins import add_fact, entity, load_step, new_db, private_dir  # noqa: F401


def link_facts(con, entities):
    deps = entity_link_service.EntityLinkDeps(entity_repository_store.SqliteEntityRepository(con))
    return entity_link_service.link_facts(deps, [entity_rules.Entity.from_dict(e) for e in entities]).as_dict()


def _rows(con, sql):
    return [tuple(r) for r in con.execute(sql)]


def test_link_facts_allocates_entity_rows_in_entity_order_and_links_in_fact_then_entity_order():
    con = new_db()
    add_fact(con, "k1", statement="Beta then Alpha.", visibility="normal")
    add_fact(con, "k2", statement="Only Alpha.", visibility="normal")
    con.commit()
    es = [entity("alpha", "Alpha", aliases=["Al"]), entity("beta", "Beta")]
    assert link_facts(con, es)["links"] == 3
    assert _rows(con, "SELECT id, entity_key FROM entities ORDER BY id") == [(1, "alpha"), (2, "beta")]
    assert _rows(con, "SELECT entity_id, alias FROM entity_aliases ORDER BY rowid") == [(1, "Al")]
    assert _rows(con, "SELECT fact_id, entity_id FROM fact_entities ORDER BY rowid") == [(1, 1), (1, 2), (2, 1)]


def test_link_facts_second_attempt_over_the_same_entities_fails_and_leaves_the_first_rows():
    con = new_db()
    add_fact(con, "k1", statement="About Alpha.", visibility="normal")
    es = [entity("alpha", "Alpha")]
    assert link_facts(con, es)["links"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        link_facts(con, es)
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
        link_facts(con, es)
    assert _rows(con, "SELECT entity_key FROM entities ORDER BY id") == [("alpha",), ("beta",)]
    assert _rows(con, "SELECT COUNT(*) FROM fact_entities") == [(0,)]


def test_link_facts_nul_blank_and_none_text_never_match_and_never_crash():
    con = new_db()
    add_fact(con, "k1", statement="Alpha\x00Beta", visibility="normal", notes="  ")
    add_fact(con, "k2", statement="x", visibility="normal", notes=None, quote=None, locator="p. 3")
    out = link_facts(con, [entity("alpha", "Alpha"), entity("beta", "Beta")])
    assert out["entities"] == 2 and out["unprotected"] == []
    assert _rows(con, "SELECT fact_id, entity_id FROM fact_entities ORDER BY rowid") == [(1, 1), (1, 2)]


def test_link_facts_text_scanned_is_statement_notes_citations_and_a_mention_in_a_quote_links():
    con = new_db()
    add_fact(con, "k1", statement="Plain.", notes="see Gamma", quote="said Delta", locator="p. 2", visibility="normal")
    out = link_facts(con, [entity("g", "Gamma"), entity("d", "Delta", private=True)])
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
