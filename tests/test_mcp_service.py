"""The MCP use case (`mcp_service`) and its pure rules (`mcp_rules`), run against in-memory fakes.

The core takes a JSON-RPC line (a str) and returns a reply (a str or None): no stdin, no stdout, no file, no
clock. Every outcome of the ports it calls (a refused write, a write that is not committed, a missing db) must
reach the caller as an error result, never be dropped (the rule for any new outcome that could be ignored).
"""
import ast
import json
import os
from types import SimpleNamespace

import pytest

import cli_parity
import fact_reads_port
import mcp_rules
import mcp_service
import new_fact
from private_git_port import PrivateGit  # noqa: F401  (documents which port the fake mimics)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION = "server-session-1"


class FakeCapture:
    """Mimics add_fact.record_fact's outcomes without a file or git."""
    def __init__(self, **outcome):
        self.calls = []
        self.outcome = outcome

    def record_fact(self, fact, data_path=None, db_path=None, allow_dirty=False):
        import add_fact_service
        self.calls.append((fact, allow_dirty))
        o = self.outcome
        if "raise_" in o:
            raise o["raise_"]
        added = o.get("added", new_fact.AddResult(ok=True, notes=o.get("notes", []), entry={"source_key": "f-abc", "visibility": "private"}, total=3))
        return add_fact_service.RecordResult(added=added, refused=o.get("refused"), repo=o.get("repo", "/r"),
                                             data_dir="/d", message="add-fact: x (low)", commit=o.get("commit", "abc123"),
                                             commit_error=o.get("commit_error"))


class FakeLookup:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, []

    def search_facts(self, terms, **filters):
        self.calls.append(("search", terms, filters))
        if self.error:
            raise self.error
        return self.rows

    def list_subjects(self, include_pending=False):
        self.calls.append(("subjects", include_pending))
        if self.error:
            raise self.error
        return self.rows


def deps(capture=None, lookup=None):
    return mcp_service.McpDeps(capture=capture or FakeCapture(), lookup=lookup or FakeLookup(), session=SimpleNamespace(session_id=SESSION))


def rpc(method, params=None, id_=1, **extra):
    msg = {"jsonrpc": "2.0", "method": method, **extra}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


def call(d, method, params=None, id_=1):
    reply = mcp_service.handle_line(d, rpc(method, params, id_))
    return None if reply is None else json.loads(reply)


def tool(d, name, arguments):
    return call(d, "tools/call", {"name": name, "arguments": arguments})["result"]


GOOD = {"statement": "Creatine monohydrate is 5 g a day.", "subject": "supplements", "trust_level": "medium",
        "recheck_by": "2027-01-01", "source_quote": "5 g daily is the standard dose"}


# ------------------------------------------------------------------ protocol

def test_initialize_negotiates_version_and_advertises_only_tools():
    r = call(deps(), "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "c"}})
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert set(r["result"]["capabilities"]) == {"tools"}
    assert r["result"]["serverInfo"]["name"] == "knowledge"
    assert "pending" in r["result"]["instructions"]


def test_initialize_with_an_unknown_version_answers_with_the_latest_supported():
    r = call(deps(), "initialize", {"protocolVersion": "1999-01-01"})
    assert r["result"]["protocolVersion"] == mcp_rules.SUPPORTED_VERSIONS[0]


def test_notifications_get_no_reply_and_do_nothing():
    d = deps()
    assert mcp_service.handle_line(d, rpc("notifications/initialized", id_=None)) is None
    assert mcp_service.handle_line(d, rpc("notifications/anything", id_=None)) is None
    assert d.capture.calls == [] and d.lookup.calls == []


def test_ping_and_unknown_method():
    assert call(deps(), "ping")["result"] == {}
    err = call(deps(), "resources/list")["error"]
    assert err["code"] == -32601


def test_an_unknown_method_notification_is_silent():
    assert mcp_service.handle_line(deps(), rpc("bogus", id_=None)) is None


@pytest.mark.parametrize("line", ["", "   ", "{", "not json", "\x00", "[1,2]", "[]", "3", '"x"', "null", "{}",
                                  '{"jsonrpc":"2.0"}', '{"jsonrpc":"1.0","method":"ping","id":1}',
                                  '{"jsonrpc":"2.0","method":5,"id":1}', '{"jsonrpc":"2.0","method":"ping","id":{}}',
                                  '{"jsonrpc":"2.0","method":"ping","id":true}'])
def test_malformed_input_never_raises_and_always_answers_with_an_error_or_nothing(line):
    reply = mcp_service.handle_line(deps(), line)
    if reply is not None:
        body = json.loads(reply)
        assert "error" in body and "result" not in body
        assert body["error"]["code"] in (-32700, -32600)


def test_parse_error_has_a_null_id():
    assert json.loads(mcp_service.handle_line(deps(), "{"))["id"] is None


def test_string_and_integer_ids_are_echoed_exactly():
    assert call(deps(), "ping", id_="abc")["id"] == "abc"
    assert call(deps(), "ping", id_=0)["id"] == 0


def test_tools_list_matches_the_catalog_and_every_schema_is_an_object():
    tools = call(deps(), "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == list(mcp_rules.TOOL_NAMES) == ["search_facts", "list_subjects", "add_fact"]
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
        assert set(t["inputSchema"].get("required", [])) <= set(t["inputSchema"]["properties"])


def test_add_fact_schema_does_not_expose_provenance_or_review_fields_the_server_owns():
    props = {t["name"]: t for t in mcp_rules.tool_list()}["add_fact"]["inputSchema"]["properties"]
    for owned in ("captured_via", "status", "source_key", "captured_at", "origin_path", "allow_dirty"):
        assert owned not in props


def test_parity_actions_are_all_registered_cli_commands():
    assert cli_parity.declared_without_entry(mcp_rules.PARITY_ACTIONS, cli_parity.ACTIONS) == []


# ------------------------------------------------------------------ add_fact

def test_add_fact_builds_a_pending_private_mcp_fact_with_the_server_session():
    d = deps()
    r = tool(d, "add_fact", GOOD)
    assert r["isError"] is False
    ((fact, allow_dirty),) = d.capture.calls
    assert allow_dirty is False
    assert (fact.captured_via, fact.session_id, fact.status, fact.visibility) == ("mcp", SESSION, "pending", "private")
    assert fact.source_quote == GOOD["source_quote"] and fact.statement == GOOD["statement"]
    assert "pending" in r["content"][0]["text"] and "f-abc" in r["content"][0]["text"]


def test_a_caller_given_session_id_wins_over_the_server_default():
    d = deps()
    tool(d, "add_fact", {**GOOD, "session_id": "mine"})
    assert d.capture.calls[0][0].session_id == "mine"


def test_visibility_can_be_asked_for_but_the_default_is_private():
    d = deps()
    tool(d, "add_fact", {**GOOD, "visibility": "normal"})
    assert d.capture.calls[0][0].visibility == "normal"


def test_no_decay_with_a_rationale_is_accepted():
    d = deps()
    args = {k: v for k, v in GOOD.items() if k != "recheck_by"}
    assert tool(d, "add_fact", {**args, "no_decay": True, "recheck_rationale": "a definition"})["isError"] is False


@pytest.mark.parametrize("bad", [
    {"statement": ""}, {"statement": "   "}, {"statement": "x\x00y"}, {"statement": 5}, {"statement": None},
    {"subject": "Not Kebab"}, {"subject": ""}, {"subject": 3},
    {"trust_level": "certain"}, {"trust_level": ["low"]},
    {"source_quote": ""}, {"source_quote": "  "}, {"source_quote": 1},
    {"no_decay": "yes"}, {"no_decay": 1}, {"is_personal": "false"},
    {"recheck_by": ""}, {"recheck_by": 20270101},
    {"kind": "nonsense"}, {"visibility": "public"},
    {"captured_via": "cli"}, {"status": "active"}, {"allow_dirty": True}, {"surprise": 1},
    {"session_id": ""}, {"session_id": 7}, {"statement": "a" * 5001},
])
def test_invalid_arguments_are_an_error_result_and_nothing_is_written(bad):
    d = deps()
    r = tool(d, "add_fact", {**GOOD, **bad})
    assert r["isError"] is True and r["content"][0]["text"]
    assert d.capture.calls == []


@pytest.mark.parametrize("arguments", [None, [], "x", 3, {}])
def test_add_fact_with_missing_or_non_object_arguments_is_an_error_not_a_crash(arguments):
    d = deps()
    r = call(d, "tools/call", {"name": "add_fact", "arguments": arguments})
    body = r.get("result") or r.get("error")
    assert (body.get("isError") is True) or ("code" in body)
    assert d.capture.calls == []


def test_missing_freshness_is_refused_with_the_domain_rule_message():
    args = {k: v for k, v in GOOD.items() if k != "recheck_by"}
    d = deps()
    r = tool(d, "add_fact", args)
    assert r["isError"] and "freshness" in r["content"][0]["text"]
    assert d.capture.calls == []


def test_a_refused_write_is_an_error_result_naming_the_reason():
    r = tool(deps(FakeCapture(refused="private repo has uncommitted changes")), "add_fact", GOOD)
    assert r["isError"] and "uncommitted" in r["content"][0]["text"]


def test_validation_errors_from_the_use_case_are_all_reported():
    added = new_fact.AddResult(ok=False, errors=["e1", "e2"], notes=["n1"])
    r = tool(deps(FakeCapture(added=added)), "add_fact", GOOD)
    text = r["content"][0]["text"]
    assert r["isError"] and "e1" in text and "e2" in text and "n1" in text


def test_a_fact_written_but_not_committed_is_an_error_that_says_it_is_in_the_file():
    r = tool(deps(FakeCapture(commit_error="index.lock exists")), "add_fact", GOOD)
    text = r["content"][0]["text"]
    assert r["isError"] and "IS in the facts file" in text and "NOT committed" in text and "index.lock" in text


def test_a_data_file_outside_git_is_a_success_that_says_it_was_not_committed():
    r = tool(deps(FakeCapture(repo=None, commit=None)), "add_fact", GOOD)
    assert r["isError"] is False and "not committed" in r["content"][0]["text"].lower()


def test_privacy_raising_is_reported():
    import privacy
    res = privacy.Resolution(visibility="private", reasons=(privacy.Reason(kind="subject-tag", detail="health", forces_private=True),))
    assert res.raised_above_request
    added = new_fact.AddResult(ok=True, entry={"source_key": "f-1", "visibility": "private"}, total=1, privacy=res)
    text = tool(deps(FakeCapture(added=added)), "add_fact", {**GOOD, "visibility": "normal"})["content"][0]["text"]
    assert "Stored as private although normal was requested" in text
    assert privacy.Resolution is not None


def test_a_port_that_raises_becomes_an_error_result_and_the_next_request_still_works():
    d = deps(FakeCapture(raise_=RuntimeError("disk on fire")))
    r = tool(d, "add_fact", GOOD)
    assert r["isError"] and "disk on fire" in r["content"][0]["text"]
    assert call(d, "ping")["result"] == {}


def test_failure_on_the_second_attempt_does_not_poison_the_first_success():
    d = deps()
    assert tool(d, "add_fact", GOOD)["isError"] is False
    d.capture.outcome["refused"] = "dirty"
    assert tool(d, "add_fact", GOOD)["isError"] is True
    assert len(d.capture.calls) == 2


# ------------------------------------------------------------------ reads

ROW = {"id": 4, "subject": "supplements", "statement": "S.", "trust_level": "low", "status": "pending"}


def test_search_passes_filters_and_returns_rows_as_json_text():
    d = deps(lookup=FakeLookup([ROW]))
    r = tool(d, "search_facts", {"terms": "creatine", "subject": "supplements", "limit": 5, "include_pending": True})
    assert r["isError"] is False
    assert json.loads(r["content"][0]["text"]) == [ROW]
    assert d.lookup.calls == [("search", "creatine", {"subject": "supplements", "limit": 5, "include_pending": True})]


def test_search_defaults_are_bounded():
    d = deps()
    tool(d, "search_facts", {"terms": "x"})
    assert d.lookup.calls[0][2]["limit"] == mcp_rules.DEFAULT_LIMIT


@pytest.mark.parametrize("arguments", [{"terms": ""}, {"terms": "  "}, {"terms": 1}, {}, {"terms": "x", "limit": 0},
                                       {"terms": "x", "limit": 10_000}, {"terms": "x", "limit": "5"},
                                       {"terms": "x", "limit": True}, {"terms": "x", "bogus": 1},
                                       {"terms": "x", "include_pending": "yes"}])
def test_search_rejects_bad_arguments_without_calling_the_port(arguments):
    d = deps()
    assert tool(d, "search_facts", arguments)["isError"] is True
    assert d.lookup.calls == []


def test_a_missing_db_is_an_error_result_that_says_to_build():
    r = tool(deps(lookup=FakeLookup(error=fact_reads_port.DatabaseNotFound("no db"))), "search_facts", {"terms": "x"})
    assert r["isError"] and "build" in r["content"][0]["text"]


def test_a_bad_full_text_query_is_an_error_result():
    r = tool(deps(lookup=FakeLookup(error=fact_reads_port.FactQueryError("syntax error near '\"'"))), "search_facts", {"terms": '"'})
    assert r["isError"] and "syntax error" in r["content"][0]["text"]


def test_rows_that_are_not_plain_json_do_not_crash_the_reply():
    class Odd:
        def __str__(self):
            return "odd"
    r = tool(deps(lookup=FakeLookup([{"v": Odd()}])), "search_facts", {"terms": "x"})
    assert r["isError"] is False and "odd" in r["content"][0]["text"]


def test_list_subjects():
    d = deps(lookup=FakeLookup([{"name": "supplements"}]))
    r = tool(d, "list_subjects", {"include_pending": True})
    assert json.loads(r["content"][0]["text"]) == [{"name": "supplements"}]
    assert d.lookup.calls == [("subjects", True)]


def test_unknown_tool_is_a_protocol_error_and_calls_nothing():
    d = deps()
    r = call(d, "tools/call", {"name": "delete_everything", "arguments": {}})
    assert r["error"]["code"] == -32602
    assert d.capture.calls == [] and d.lookup.calls == []


# ------------------------------------------------------------------ no I/O in the core

CORE = ("mcp_rules.py", "mcp_port.py", "mcp_service.py")
FORBIDDEN_IMPORTS = {"sys", "os", "io", "sqlite3", "subprocess", "socket", "pathlib", "shutil", "tempfile", "uuid",
                     "secrets", "random", "time", "datetime", "threading", "logging", "urllib", "http", "select"}
FORBIDDEN_CALLS = {"print", "open", "input", "exit", "quit", "eval", "exec", "__import__"}


@pytest.mark.parametrize("name", CORE)
def test_the_core_has_no_inputs_or_outputs(name):
    tree = ast.parse(open(os.path.join(REPO, name), encoding="utf-8").read())
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & FORBIDDEN_IMPORTS, imported & FORBIDDEN_IMPORTS
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & FORBIDDEN_CALLS, called & FORBIDDEN_CALLS


def test_every_outcome_field_of_record_fact_is_handled_by_the_service():
    """A new field on RecordResult that the MCP reply ignores would be silently dropped: fail until it is handled."""
    import dataclasses
    import add_fact_service
    handled = {"added", "refused", "repo", "data_dir", "message", "commit", "detached", "commit_error"}
    assert {f.name for f in dataclasses.fields(add_fact_service.RecordResult)} == handled
    src = open(os.path.join(REPO, "mcp_rules.py"), encoding="utf-8").read()
    for used in ("refused", "commit_error", "added", "repo"):
        assert used in src
