"""Issue #6, default status filter: knowledge.py query functions and commands vs pending facts.

The first version of this file pinned the behavior BEFORE the filter existed (everything visible,
pending included); see the commit history. Now: facts/search/subjects show active facts only unless
--include-pending or --status says otherwise; show and history work for any status. Fixture: test_knowledge's db plus
fact 6 (pending, protein), fact 7 (pending, in a subject tagged private), all from the real schema.sql.
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_knowledge import Env as BaseEnv, ok, L1, L2, L3, L4, L5, lib  # noqa: E402,F401


class Env(BaseEnv):
    def __init__(self, root, with_db=True):
        super().__init__(root, with_db=with_db)
        if with_db:
            self.sql("INSERT INTO subjects (id, name, domain, parent_id, private) VALUES (5, 'diary', 'personal', NULL, 1)")
            self.sql("""INSERT INTO facts (id, subject_id, statement, is_personal, trust_level, status, source_key, volatility) VALUES
                          (6, 2, 'Pending protein lemma about whey', 0, 'low', 'pending', 'p-six', 'static'),
                          (7, 5, 'Pending diary entry about whey', 1, 'low', 'pending', 'p-seven', 'static')""")

    def sql(self, q, *params):
        con = sqlite3.connect(self.db)
        con.execute(q, params)
        con.commit()
        con.close()


L6 = "#6     [protein] (low) [pending]  Pending protein lemma about whey\n"
L7 = "#7     [diary] (low) [pending]  Pending diary entry about whey\n"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---------------------------------------------------------------- default: active only

def test_facts_default_hides_pending(env):
    assert ok(env.cli("facts")) == L1 + L2 + L3


def test_facts_include_pending_adds_pending_but_not_superseded_or_retracted(env):
    assert ok(env.cli("facts", "--include-pending")) == L1 + L2 + L3 + L6 + L7


def test_facts_status_pending_works_without_the_flag_and_status_overrides_it(env):
    assert ok(env.cli("facts", "--status", "pending")) == L6 + L7
    assert ok(env.cli("facts", "--status", "pending", "--include-pending")) == L6 + L7
    assert ok(env.cli("facts", "--status", "active", "--include-pending")) == L1 + L2 + L3
    assert ok(env.cli("facts", "--status", "retracted", "--include-pending")) == L5


def test_facts_json_follows_the_same_filter(env):
    assert [r["id"] for r in json.loads(ok(env.cli("facts", "--json")))] == [1, 2, 3]
    assert [r["id"] for r in json.loads(ok(env.cli("facts", "--include-pending", "--json")))] == [1, 2, 3, 6, 7]
    assert {r["status"] for r in json.loads(ok(env.cli("facts", "--status", "pending", "--json")))} == {"pending"}


def test_search_hides_pending_by_default(env):
    assert ok(env.cli("search", "whey")) == "No matches.\n"
    assert set(ok(env.cli("search", "whey", "--include-pending")).splitlines(keepends=True)) == {L6, L7}
    assert set(ok(env.cli("search", "whey", "--status", "pending")).splitlines(keepends=True)) == {L6, L7}
    assert [r["id"] for r in json.loads(ok(env.cli("search", "lemma", "--include-pending", "--json")))] == [6]


def test_subjects_count_active_facts_by_default(env):
    out = ok(env.cli("subjects"))
    assert "  protein                             (health-and-fitness, 2 facts)\n" in out
    assert "diary                               (personal, 0 facts)\n" in out
    out = ok(env.cli("subjects", "--include-pending"))
    assert "  protein                             (health-and-fitness, 3 facts)\n" in out
    assert "diary                               (personal, 1 facts)\n" in out
    counts = {s["name"]: s["n_facts"] for s in json.loads(ok(env.cli("subjects", "--include-pending", "--json")))}
    assert counts["protein"] == 3 and counts["diary"] == 1


def test_show_by_explicit_id_displays_any_status(env):
    out = ok(env.cli("show", "6"))
    assert out.splitlines()[0] == "Fact #6  [protein]  trust=low  personal=False  visibility=private  status=pending"
    assert json.loads(ok(env.cli("show", "7", "--json")))["status"] == "pending"
    assert "status=superseded" in ok(env.cli("show", "4"))
    assert "status=retracted" in ok(env.cli("show", "5"))


def test_library_functions(lib, env):
    con = lib.connect()
    assert [r["id"] for r in lib.list_facts(con)] == [1, 2, 3]
    assert [r["id"] for r in lib.list_facts(con, include_pending=True)] == [1, 2, 3, 6, 7]
    assert [r["id"] for r in lib.list_facts(con, status="pending")] == [6, 7]
    assert [r["id"] for r in lib.list_facts(con, status="pending", include_pending=False)] == [6, 7]
    assert lib.search_facts(con, "whey") == []
    assert {r["id"] for r in lib.search_facts(con, "whey", include_pending=True)} == {6, 7}
    assert {r["id"] for r in lib.search_facts(con, "protein", status="superseded")} == {4}
    assert lib.get_fact(con, 6)["status"] == "pending"  # not filtered
    assert lib.get_fact(con, 4)["status"] == "superseded"
    counts = {s["name"]: s["n_facts"] for s in lib.list_subjects(con)}
    assert counts["diary"] == 0 and counts["protein"] == 2 and counts["emptysubject"] == 0
    assert {s["name"]: s["n_facts"] for s in lib.list_subjects(con, include_pending=True)}["diary"] == 1
    con.close()


def test_no_pending_facts_means_include_pending_changes_nothing(tmp_path):
    e = BaseEnv(tmp_path)
    assert ok(e.cli("facts", "--include-pending")) == ok(e.cli("facts"))
    assert ok(e.cli("subjects", "--include-pending")) == ok(e.cli("subjects"))


def test_history_of_a_pending_fact_works(env):
    env.sql("""INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, status, visibility)
               VALUES (6, 'p-six', 1, '2026-01-01T00:00:00+00:00', 'cli', 'Pending protein lemma about whey', 'low', 'pending', 'private')""")
    assert "Fact #6" in ok(env.cli("history", "6"))


def test_empty_db_pins(tmp_path):
    e = BaseEnv(tmp_path, with_db=False)
    con = sqlite3.connect(e.db)
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")) as f:
        con.executescript(f.read())
    con.close()
    assert ok(e.cli("facts")) == "No matches.\n"
    assert ok(e.cli("search", "x")) == "No matches.\n"
    assert ok(e.cli("subjects")) == ""
    assert json.loads(ok(e.cli("facts", "--json"))) == []
