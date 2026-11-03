"""The stdio shell (`mcp_server`) end to end: a real subprocess, a real throwaway private git repo, real lines on stdin.

The core never reads or writes; this proves the one module that does keeps the protocol's output stream clean
(only JSON replies) and that a fact added through it lands pending, private, with mcp provenance, and committed.
"""
import io
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_server  # noqa: E402
from test_private_git import GitEnv  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACT = {"statement": "Creatine monohydrate is 5 g a day.", "subject": "supplements", "trust_level": "medium",
        "recheck_by": "2027-01-01", "source_quote": "5 g daily is the standard dose"}


def lines(*messages):
    return "".join(json.dumps(m) + "\n" for m in messages)


def run_server(env, text):
    return subprocess.run([sys.executable, os.path.join(REPO, "mcp_server.py")], input=text, capture_output=True,
                          text=True, env=env.env, cwd=REPO, timeout=60)


def test_a_session_over_real_stdio_adds_a_pending_private_committed_fact(tmp_path):
    g = GitEnv(tmp_path)
    r = run_server(g, lines(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "add_fact", "arguments": FACT}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_facts", "arguments": {"terms": "creatine"}}},
    ))
    assert r.returncode == 0, r.stderr
    replies = [json.loads(line) for line in r.stdout.splitlines()]     # every stdout line is a JSON reply, nothing else
    assert [x["id"] for x in replies] == [1, 2, 3]                     # the notification got none
    assert replies[1]["result"]["isError"] is False, replies[1]
    assert replies[2]["result"]["isError"] is True and "build" in replies[2]["result"]["content"][0]["text"]  # no db yet
    (entry,) = json.load(open(g.facts))
    assert (entry["captured_via"], entry["status"], entry["visibility"]) == ("mcp", "pending", "private")
    assert entry["session_id"].startswith("mcp-") and entry["source_quote"] == FACT["source_quote"]
    assert g.status() == "" and g.log()[0] == "add-fact: supplements (medium)"


def test_a_dirty_private_repo_is_refused_through_the_server_and_nothing_is_written(tmp_path):
    g = GitEnv(tmp_path)
    with open(os.path.join(g.data_dir, "other.json"), "w") as f:
        f.write('["edited"]\n')
    head = g.head()
    r = run_server(g, lines({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "add_fact", "arguments": FACT}}))
    (reply,) = [json.loads(line) for line in r.stdout.splitlines()]
    assert reply["result"]["isError"] is True and "Refused" in reply["result"]["content"][0]["text"]
    assert not os.path.exists(g.facts) and g.head() == head


def test_garbage_and_blank_lines_do_not_end_the_session(tmp_path):
    g = GitEnv(tmp_path)
    r = run_server(g, "\n{nope\n\x00\n" + lines({"jsonrpc": "2.0", "id": 9, "method": "ping"}))
    replies = [json.loads(line) for line in r.stdout.splitlines()]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 9, "result": {}}
    assert all("error" in x for x in replies[:-1])


def test_serve_keeps_going_after_the_output_stream_raises_once_and_stops_on_a_closed_pipe():
    class Boom(io.StringIO):
        def __init__(self, exc):
            super().__init__()
            self.exc, self.calls = exc, 0

        def write(self, s):
            self.calls += 1
            raise self.exc

    import appctx
    app = appctx.app()
    err = io.StringIO()
    w = Boom(ValueError("bad bytes"))
    mcp_server.serve(app, io.StringIO(lines({"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"})), w, "s", err)
    assert w.calls == 2 and "bad bytes" in err.getvalue()
    w = Boom(BrokenPipeError())
    mcp_server.serve(app, io.StringIO(lines({"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"})), w, "s", err)
    assert w.calls == 1
