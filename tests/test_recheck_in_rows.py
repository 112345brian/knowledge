"""Search and list rows carry `recheck_by`, so a caller (the MCP freshness skill, `search --json`) can tell a stale fact from a current one.

Both read layers (fact_queries and the mode-filtered modes_store) must return the same column, a fact without a recheck date
carries None (not a missing key), and the mode rules still hide private facts.
"""
import json

import fact_queries
import modes
import modes_store
from test_applies_to import _con


def _db():
    con = _con()
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, recheck_by, visibility, source_key) "
                "VALUES (1, 1, 'Creatine helps.', 'low', 'unreviewed', '2031-01-02', 'normal', 'k1')")
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, visibility, source_key) "
                "VALUES (2, 1, 'Creatine is safe.', 'low', 'unreviewed', 'normal', 'k2')")
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, recheck_by, visibility, source_key) "
                "VALUES (3, 1, 'Creatine secret.', 'low', 'unreviewed', '2031-05-06', 'private', 'k3')")
    return con


def test_rows_carry_recheck_by_in_both_layers():
    con = _db()
    plain = {r["id"]: r["recheck_by"] for r in fact_queries.list_facts(con)}
    assert plain[1] == "2031-01-02" and plain[2] is None
    assert {r["id"]: r["recheck_by"] for r in fact_queries.search_facts(con, "creatine")}[1] == "2031-01-02"
    normal = modes.Session(mode=modes.Mode.normal)
    private = modes.Session(mode=modes.Mode.private)
    assert {r["id"]: r["recheck_by"] for r in modes_store.list_facts(normal, con)} == {1: "2031-01-02", 2: None}
    assert {r["id"]: r["recheck_by"] for r in modes_store.search_facts(normal, con, "creatine")} == {1: "2031-01-02", 2: None}
    assert {r["id"]: r["recheck_by"] for r in modes_store.list_facts(private, con)}[3] == "2031-05-06"
    assert {r["id"]: r["recheck_by"] for r in modes_store.search_facts(private, con, "creatine")}[3] == "2031-05-06"


def test_normal_mode_still_hides_the_private_facts_recheck_date():
    con = _db()
    normal = modes.Session(mode=modes.Mode.normal)
    assert "2031-05-06" not in json.dumps(modes_store.list_facts(normal, con) + modes_store.search_facts(normal, con, "creatine"))
