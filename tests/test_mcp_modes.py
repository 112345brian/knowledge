"""The MCP server is held to a mode (`mcp_mode` in the config file: off / normal / private; the rules are in `modes`).

normal (the default) never shows a private fact or a private subject; private shows everything; off refuses every
read and write, over the real stdio shell as well as in the adapter. The default is normal so a fresh install exposes
the least. A scan fails if the shell is wired to the unfiltered `queries` again.
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_files  # noqa: E402
import mcp_moded_store  # noqa: E402
import modes  # noqa: E402
import paths  # noqa: E402
from fact_reads_port import DatabaseNotFound  # noqa: E402
from test_mcp_server import FACT, lines, run_server  # noqa: E402
from test_private_git import GitEnv  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "knowledge.db")
    con = sqlite3.connect(path)
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (id, name, domain, private) VALUES (1, 'open', 'g', 0), (2, 'secret', 'g', 1)")
    for fid, sid, text, vis in ((1, 1, "Creatine open fact.", "normal"), (2, 1, "Creatine private fact.", "private"),
                                (3, 2, "Creatine on a private subject.", "normal")):
        con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, recheck_by, visibility, source_key) "
                    "VALUES (?, ?, ?, 'low', 'unreviewed', '2031-01-02', ?, ?)", (fid, sid, text, vis, f"k{fid}"))
    con.commit()
    con.close()
    return path


def lookup(db, mode):
    return mcp_moded_store.ModedFactLookup(db, modes.Session(mode=mode))


def test_normal_mode_search_hides_private_facts_and_subjects(db):
    rows = lookup(db, "normal").search_facts("creatine", limit=10)
    assert [r["id"] for r in rows] == [1] and rows[0]["recheck_by"] == "2031-01-02"
    assert [s["name"] for s in lookup(db, "normal").list_subjects()] == ["open"]


def test_private_mode_sees_everything(db):
    assert sorted(r["id"] for r in lookup(db, "private").search_facts("creatine", limit=10)) == [1, 2, 3]
    assert sorted(s["name"] for s in lookup(db, "private").list_subjects()) == ["open", "secret"]


def test_off_mode_refuses_reads_even_without_a_database(tmp_path):
    for fn in (lambda lk: lk.search_facts("creatine", limit=10), lambda lk: lk.list_subjects()):
        with pytest.raises(modes.ModeOff):
            fn(lookup(str(tmp_path / "missing.db"), "off"))


def test_a_missing_database_is_still_reported_as_missing_when_on(tmp_path):
    with pytest.raises(DatabaseNotFound):
        lookup(str(tmp_path / "missing.db"), "normal").search_facts("x", limit=10)


class Recorder:
    def __init__(self):
        self.calls = []

    def record_fact(self, fact, data_path=None, db_path=None, allow_dirty=False):
        self.calls.append(fact)
        return "recorded"


@pytest.mark.parametrize("mode,allowed", [("off", False), ("normal", True), ("private", True)])
def test_writes_are_refused_only_when_off(mode, allowed):
    inner = Recorder()
    capture = mcp_moded_store.ModedCapture(inner, modes.Session(mode=mode))
    if allowed:
        assert capture.record_fact("f") == "recorded" and inner.calls == ["f"]
    else:
        with pytest.raises(modes.ModeOff):
            capture.record_fact("f")
        assert inner.calls == []


def test_the_config_defaults_to_normal_and_rejects_an_unknown_mode(tmp_path, monkeypatch):
    path = config_files.write_config(str(tmp_path / "c" / "config.toml"), str(tmp_path / "db"), str(tmp_path / "data"))
    monkeypatch.setenv(paths.CONFIG_ENV_VAR, path)
    assert paths.load().mcp_mode == "normal"
    monkeypatch.setenv("KNOWLEDGE_MCP_MODE", "private")
    assert paths.load().mcp_mode == "private"
    monkeypatch.setenv("KNOWLEDGE_MCP_MODE", "everything")
    with pytest.raises(paths.ConfigError):
        paths.load()


def test_an_off_server_refuses_add_and_search_over_stdio_and_writes_nothing(tmp_path):
    g = GitEnv(tmp_path)
    g.env = {**g.env, "KNOWLEDGE_MCP_MODE": "off"}
    head = g.head()
    r = run_server(g, lines(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "add_fact", "arguments": FACT}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_facts", "arguments": {"terms": "creatine"}}},
    ))
    replies = [json.loads(line) for line in r.stdout.splitlines()]
    assert all(x["result"]["isError"] is True for x in replies), replies
    assert not os.path.exists(g.facts) and g.head() == head


def test_the_shell_is_wired_to_the_moded_adapters_not_the_unfiltered_queries():
    src = open(os.path.join(REPO, "mcp_server.py"), encoding="utf-8").read()
    assert "app.queries" not in src and "app.mcp_lookup" in src and "app.mcp_capture" in src


def test_private_mode_search_keeps_the_tool_contract_active_only_unless_pending_is_asked(db):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, visibility, source_key, status) "
                "VALUES (4, 1, 'Creatine pending fact.', 'low', 'unreviewed', 'normal', 'k4', 'pending'), "
                "(5, 1, 'Creatine superseded fact.', 'low', 'unreviewed', 'normal', 'k5', 'superseded')")
    con.commit()
    con.close()
    for mode in ("normal", "private"):
        lk = lookup(db, mode)
        assert 4 not in [r["id"] for r in lk.search_facts("creatine", limit=10)], mode
        asked = [r["id"] for r in lk.search_facts("creatine", limit=10, include_pending=True)]
        assert 4 in asked and 5 not in asked, mode
    assert len(lookup(db, "private").search_facts("creatine", limit=2, include_pending=True)) == 2
