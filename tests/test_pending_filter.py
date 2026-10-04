"""Issue #6, default status filter: knowledge.py query functions and commands vs pending facts.

Section 1 pinned the behavior BEFORE the filter existed (everything visible, pending included);
it is rewritten alongside the change, see the commit history. Fixture: test_knowledge's db plus
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
            self.sql("""INSERT INTO facts (id, subject_id, statement, is_personal, trust_level, status, source_key) VALUES
                          (6, 2, 'Pending protein lemma about whey', 0, 'low', 'pending', 'p-six'),
                          (7, 5, 'Pending diary entry about whey', 1, 'low', 'pending', 'p-seven')""")

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


# ---------------------------------------------------------------- section 1: old behavior

def test_facts_lists_pending_too(env):
    assert ok(env.cli("facts")) == L1 + L2 + L3 + L4 + L5 + L6 + L7


def test_facts_status_pending_is_not_a_choice_yet(env):
    assert env.cli("facts", "--status", "pending").returncode == 2


def test_search_finds_pending_too(env):
    assert set(ok(env.cli("search", "whey")).splitlines(keepends=True)) == {L6, L7}


def test_subjects_count_pending_too(env):
    out = ok(env.cli("subjects"))
    assert "  protein                             (health-and-fitness, 3 facts)\n" in out
    assert "diary                               (personal, 1 facts)\n" in out


def test_show_displays_a_pending_fact_with_its_status(env):
    out = ok(env.cli("show", "6"))
    assert out.splitlines()[0] == "Fact #6  [protein]  trust=low  personal=False  visibility=private  status=pending"


def test_library_functions_see_pending(lib, env):
    con = lib.connect()
    assert [r["id"] for r in lib.list_facts(con)] == [1, 2, 3, 4, 5, 6, 7]
    assert {r["id"] for r in lib.search_facts(con, "whey")} == {6, 7}
    assert lib.get_fact(con, 6)["status"] == "pending"
    assert {s["name"]: s["n_facts"] for s in lib.list_subjects(con)}["diary"] == 1
    con.close()


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
