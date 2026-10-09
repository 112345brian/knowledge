"""Issue #33: the local inbox (inbox.py, cli_inbox.py). The server runs on an ephemeral port against
fixture dbs and temp data/git dirs; nothing here touches real knowledge-private data or the live db."""
import ast
import hashlib
import html
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest
import typer.main

from test_fact_revisions import world, entry, T1, T2, T3  # noqa: F401  (world is a fixture)
from test_review import GIT_ENV, git, make_repo, pend

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def ib(world, monkeypatch):
    for m in ("privacy_store", "review", "review_store", "review_rules", "lifecycle", "lifecycle_rules", "lifecycle_store", "inbox", "cli_inbox"):
        sys.modules.pop(m, None)
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    import inbox
    import lifecycle
    import review
    import privacy
    import privacy_store
    world.inbox, world.lifecycle, world.review, world.privacy = inbox, lifecycle, review, privacy
    servers = []

    def rebuild_db():
        con = world.build()
        if os.path.exists(world.env.db):
            os.unlink(world.env.db)
        out = __import__("sqlite3").connect(world.env.db)
        con.backup(out)
        out.close()
        return world.env.db

    def start(rules=None, repo=True, snapshot=True):
        if rules is not None:
            privacy_store.save_rules(rules, privacy_store.rules_path(world.env.data_dir))
        if repo:
            make_repo(world)
        db = rebuild_db() if snapshot else world.env.db
        srv = inbox.make_server(db, world.env.data_dir)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        world.srv = srv
        return srv

    world.start, world.rebuild_db = start, rebuild_db
    yield world
    for s in servers:
        s.shutdown()
        s.server_close()


def call(srv, method="GET", path="/", form=None, host=None, headers=None, token=True, raw=None, accept_json=True,
         content_length=None):
    h = dict(headers or {})
    h["Host"] = host or f"127.0.0.1:{srv.server_address[1]}"
    body = raw
    if form is not None:
        data = {**({"token": srv.token} if token is True else ({"token": token} if token else {})), **form}
        body = urllib.parse.urlencode(data)
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if accept_json and method == "POST":
        h["Accept"] = "application/json"
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=30)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items():
        c.putheader(k, v)
    if body is not None:
        b = body.encode() if isinstance(body, str) else body
        c.putheader("Content-Length", str(len(b) if content_length is None else content_length))
    c.endheaders(body.encode() if isinstance(body, str) else body)
    r = c.getresponse()
    text = r.read().decode("utf-8")
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, hdrs, text


def act(srv, **form):
    status, _, text = call(srv, "POST", "/action", form)
    return status, json.loads(text)


def items(result):
    return [(i["ref"], i["outcome"], i["message"]) for i in result["items"]]


def hashes(w):
    out = {}
    for n in os.listdir(w.env.data_dir):
        if n.endswith(".json"):
            out[n] = hashlib.sha256(open(os.path.join(w.env.data_dir, n), "rb").read()).hexdigest()
    return out


def n_commits(w):
    return int(git(w.env.data_dir, "rev-list", "--count", "HEAD"))


def log_count(w):
    return len(w.log_lines()) if os.path.exists(w.log) else 0


def seed(w, *extra):
    w.seed(general=[pend("p1", date_added=T1, visibility="normal", captured_via="mcp", session_id="s9",
                         captured_at=T1, source_quote="she said so"),
                    pend("p2", date_added=T2), entry("a1", date_added=T1, status="active"), *extra])


# ---------------------------------------------------------------- list

def test_page_lists_pending_with_provenance_rule_and_count(ib):
    w = ib
    seed(w)
    srv = w.start(rules=w.privacy.Rules(keywords=("grace",)))
    status, hdrs, page = call(srv)
    assert status == 200 and "text/html" in hdrs["content-type"]
    assert "Pending facts: <span class=\"count\">2</span> pending" in page
    assert "Pending p1." in page and "Pending p2." in page and 'id="fact-a1"' not in page
    assert page.index("Pending p1.") < page.index("Pending p2.")  # oldest first
    assert "via mcp" in page and "session s9" in page and "she said so" in page and T1 in page
    assert "no privacy rule applies" in page            # p1 is normal, no rule
    assert "private by request or default" in page      # p2 is stored private, no rule
    assert "rebuild" in page.lower()
    assert page.count('name="token" value="%s"' % srv.token) >= 2


def test_page_names_the_rule_behind_private(ib):
    w = ib
    w.seed(general=[pend("p1", "Call Grace tomorrow.", visibility="normal"), pend("p2", "Plain.", visibility="normal")])
    srv = w.start(rules=w.privacy.Rules(keywords=("grace",)))
    _, _, page = call(srv)
    assert "private by rule: keyword: the statement contains the listed word &#x27;grace&#x27;" in page
    w2 = w.privacy.Rules(subject_tags={"alpha": "private"})
    srv2 = w.start(rules=w2, repo=False)
    assert "subject &#x27;alpha&#x27; is tagged private" in call(srv2)[2]


def test_empty_inbox(ib):
    w = ib
    w.seed(general=[entry("a1", status="active")])
    srv = w.start()
    _, _, page = call(srv)
    assert "<span class=\"count\">0</span> pending" in page and "Nothing is pending" in page
    assert "Approve all" not in page


def test_missing_db_is_an_error_banner_not_a_crash(ib):
    w = ib
    w.seed(general=[pend("p1")])
    srv = w.start(snapshot=False)
    status, _, page = call(srv)
    assert status == 200 and "run `knowledge.py build` first" in page


def test_500_pending_unicode_and_very_long_statements(ib):
    w = ib
    facts = [pend(f"k{i:03d}", f"Fait numéro {i} ☕ 日本語", date_added=T1) for i in range(498)]
    facts += [pend("kbig", "L" * 200_000), pend("kuni", "Zażółć gęślą jaźń 🎵")]
    w.seed(general=facts)
    srv = w.start(repo=False)
    t0 = time.time()
    status, _, page = call(srv)
    assert status == 200 and time.time() - t0 < 20
    assert '<span class="count">500</span> pending' in page
    assert "Fait numéro 7 ☕ 日本語" in page and "Zażółć gęślą jaźń 🎵" in page and "L" * 200_000 in page


def test_list_overlays_current_state_not_the_stale_snapshot(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert w.review.approve("p1", data_dir=w.env.data_dir, commit=False).ok     # approved after the snapshot
    assert w.rs.append_revision("p2", {"statement": "Edited since the rebuild."}, "r", "cli", data_dir=w.env.data_dir).ok
    _, _, page = call(srv)
    assert "Pending p1." not in page and "Edited since the rebuild." in page
    assert '<span class="count">1</span> pending' in page and "already reviewed" in page


def test_pending_in_files_but_not_in_snapshot_is_flagged(ib):
    w = ib
    seed(w)
    srv = w.start()
    w.seed(general=[pend("p1", date_added=T1), pend("p2", date_added=T2), pend("late", date_added=T3)])
    assert "1 more pending fact(s) are in the data files but not in this snapshot" in call(srv)[2]


# ---------------------------------------------------------------- approve / reject

def test_approve_appends_a_revision_commits_once_and_keeps_entries_unchanged(ib):
    w = ib
    seed(w)
    srv = w.start()
    before, commits = hashes(w), n_commits(w)
    status, res = act(srv, action="approve", ref="p1")
    assert status == 200 and res["ok"] and items(res) == [("p1", "approved", "")] and res["commit"]
    assert hashes(w) == before and n_commits(w) == commits + 1
    rec = json.loads(w.log_lines()[-1])
    assert (rec["source_key"], rec["status"], rec["changed_via"], rec["revision"]) == ("p1", "active", "inbox", 2)
    assert git(w.env.data_dir, "status", "--porcelain") == ""
    assert "Pending p1." not in call(srv)[2]                       # not offered again, though the db is stale
    assert [(h["revision"], h["status"]) for h in w.rs.get_history(w.build(), "p1")] == [(1, "pending"), (2, "active")]


def test_reject_needs_a_reason_and_shows_in_history(ib):
    w = ib
    seed(w)
    srv = w.start()
    status, res = act(srv, action="reject", ref="p1", reason="")
    assert status == 409 and not res["ok"] and "reason is required" in res["errors"][0] and log_count(w) == 0
    status, res = act(srv, action="reject", ref="p1", reason="not true")
    assert status == 200 and items(res) == [("p1", "rejected", "")]
    hist = w.rs.get_history(w.build(), "p1")
    assert (hist[-1]["status"], hist[-1]["change_reason"]) == ("retracted", "not true")


def test_unknown_ref_is_reported_and_writes_nothing(ib):
    w = ib
    seed(w)
    srv = w.start()
    for action in ("approve", "reject", "edit_fact", "make_private"):
        status, res = act(srv, action=action, ref="nope", reason="x", statement="y")
        assert status == 409 and not res["ok"], action
    assert items(act(srv, action="approve", ref="nope")[1])[0][1] == "unknown"
    assert log_count(w) == 0 and n_commits(w) == 1


def test_bulk_approve_approves_exactly_the_facts_shown(ib):
    w = ib
    seed(w)
    srv = w.start()
    w.seed(general=[pend("p1", date_added=T1), pend("p2", date_added=T2), pend("late", date_added=T3)])
    git(w.env.data_dir, "add", "-A")
    git(w.env.data_dir, "commit", "-q", "-m", "a fact arrives after the snapshot")
    _, _, page = call(srv)
    refs = re.search(r'name="refs" value="([^"]*)"', page).group(1)
    assert refs == "p1,p2" and "Approve all 2 shown" in page
    status, res = act(srv, action="approve_shown", refs=refs)
    assert status == 200 and [i[:2] for i in items(res)] == [("p1", "approved"), ("p2", "approved")]
    states = w.review.current_states(w.env.data_dir)
    assert states["late"]["status"] == "pending"                    # never seen by the user: untouched
    assert n_commits(w) == 3                                         # init, the late fact, ONE batch commit
    assert not act(srv, action="approve_shown", refs="")[1]["ok"]


def test_approved_by_cli_while_the_page_is_open_is_a_clean_message(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert w.review.approve("p1", data_dir=w.env.data_dir).ok       # "the CLI"
    status, res = act(srv, action="approve", ref="p1")
    assert status == 200 and res["ok"] and items(res) == [("p1", "skipped", "already approved")]
    assert log_count(w) == 1


def test_retracted_after_listing_is_never_reactivated_or_edited(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert w.review.reject("p1", "gone", data_dir=w.env.data_dir).ok
    assert items(act(srv, action="approve", ref="p1")[1]) == [("p1", "skipped", "already rejected")]
    status, res = act(srv, action="edit_fact", ref="p1", reason="r", statement="Revived?")
    assert status == 200 and items(res) == [("p1", "skipped", "already rejected: nothing changed")]
    assert w.review.current_states(w.env.data_dir)["p1"]["status"] == "retracted" and log_count(w) == 1


def test_concurrent_clicks_on_one_fact_write_once(ib):
    w = ib
    seed(w)
    srv = w.start()
    out = []

    def click():
        out.append(act(srv, action="approve", ref="p1")[1])
    threads = [threading.Thread(target=click) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    outcomes = sorted(items(r)[0][1] for r in out)
    assert outcomes == ["approved"] + ["skipped"] * 5 and all(items(r)[0][2] in ("", "already approved") for r in out)
    assert log_count(w) == 1 and n_commits(w) == 2 and git(w.env.data_dir, "status", "--porcelain") == ""


def test_concurrent_clicks_on_different_facts_lose_no_writes(ib):
    w = ib
    w.seed(general=[pend(f"k{i}", date_added=T1) for i in range(8)])
    srv = w.start()
    results = []
    threads = [threading.Thread(target=lambda i=i: results.append(act(srv, action="approve", ref=f"k{i}")[1])) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r["ok"] for r in results) and log_count(w) == 8 and n_commits(w) == 9
    assert git(w.env.data_dir, "status", "--porcelain") == ""


def test_concurrent_with_a_cli_process_loses_no_writes(ib):
    w = ib
    w.seed(general=[pend(f"k{i}", date_added=T1) for i in range(6)])
    srv = w.start(repo=False)
    code = ("import sys; sys.path.insert(0, %r); import review; "
            "[review.approve(k, data_dir=%r, commit=False) for k in ('k0','k2','k4')]") % (REPO, w.env.data_dir)
    env = {**os.environ, "KNOWLEDGE_PRIVATE_DIR": w.env.private}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env)
    for k in ("k1", "k3", "k5"):
        assert act(srv, action="approve", ref=k)[1]["ok"]
    assert proc.wait(timeout=60) == 0
    assert {s["status"] for s in w.review.current_states(w.env.data_dir).values()} == {"active"}
    assert log_count(w) == 6


def test_dirty_private_tree_shows_an_error_and_writes_nothing(ib):
    w = ib
    seed(w)
    srv = w.start()
    with open(os.path.join(w.env.data_dir, "scratch.txt"), "w") as f:
        f.write("x")
    commits = n_commits(w)
    for form in ({"action": "approve", "ref": "p1"}, {"action": "reject", "ref": "p1", "reason": "r"},
                 {"action": "edit_fact", "ref": "p1", "reason": "r", "statement": "New."},
                 {"action": "make_private", "ref": "p1"}, {"action": "approve_shown", "refs": "p1,p2"}):
        status, res = act(srv, **form)
        assert status == 409 and not res["ok"] and "uncommitted changes" in " ".join(res["errors"]), form
    assert log_count(w) == 0 and n_commits(w) == commits
    status, _, page = call(srv, "POST", "/action", {"action": "approve", "ref": "p1"}, accept_json=False)
    assert status == 409 and "uncommitted changes" in page and "not done" in page


def test_second_attempt_after_the_tree_turns_dirty_is_refused(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert act(srv, action="approve", ref="p1")[1]["ok"]
    open(os.path.join(w.env.data_dir, "stray"), "w").write("x")
    status, res = act(srv, action="approve", ref="p2")
    assert status == 409 and log_count(w) == 1


# ---------------------------------------------------------------- edit

def test_edit_appends_a_revision_with_reason_and_shows_in_history(ib):
    w = ib
    seed(w)
    srv = w.start()
    before = hashes(w)
    status, res = act(srv, action="edit_fact", ref="p1", reason="fixed typo", statement="  Better p1.  ", trust_level="high",
                      trust_rationale="checked", recheck_by="2027-01-31", notes="n")
    assert status == 200 and res["ok"] and res["commit"] and hashes(w) == before
    rec = json.loads(w.log_lines()[-1])
    assert (rec["statement"], rec["trust_level"], rec["trust_rationale"], rec["recheck_by"], rec["notes"]) == \
        ("Better p1.", "high", "checked", "2027-01-31", "n")
    assert rec["status"] == "pending" and rec["change_reason"] == "fixed typo" and rec["changed_via"] == "inbox"
    con = w.build()
    assert con.execute("SELECT s.name FROM facts f JOIN subjects s ON s.id=f.subject_id WHERE f.source_key='p1'").fetchone()[0] == "alpha"
    assert w.rs.get_history(con, "p1")[-1]["change_reason"] == "fixed typo"
    assert "Better p1." in call(srv)[2]


def test_edit_clears_optional_fields_with_blank_and_leaves_missing_ones(ib):
    w = ib
    w.seed(general=[pend("p1", notes="old note", recheck_by="2027-01-01")])
    srv = w.start()
    res = act(srv, action="edit_fact", ref="p1", reason="r", notes="", recheck_by="")[1]
    assert res["ok"]
    rec = json.loads(w.log_lines()[-1])
    assert rec["notes"] is None and rec["recheck_by"] is None and rec["statement"] == "Pending p1."


@pytest.mark.parametrize("form,needle", [
    ({"reason": "r", "statement": "   "}, "blank"),
    ({"reason": "", "statement": "x"}, "reason is required"),
    ({"reason": "r", "trust_level": "bogus"}, "trust level"),
    ({"reason": "r", "recheck_by": "soon"}, "YYYY-MM-DD"),
    ({"reason": "r"}, "nothing to edit"),
    ({"reason": "r", "statement": "Pending p1."}, "no field would change"),
])
def test_edit_refusals_write_nothing(ib, form, needle):
    w = ib
    seed(w)
    srv = w.start()
    status, res = act(srv, action="edit_fact", ref="p1", **form)
    assert status == 409 and needle in " ".join(res["errors"]) and log_count(w) == 0 and n_commits(w) == 1


def test_edit_that_trips_a_privacy_rule_raises_visibility_in_the_same_revision(ib):
    w = ib
    seed(w)
    srv = w.start(rules=w.privacy.Rules(keywords=("grace",)))
    res = act(srv, action="edit_fact", ref="p1", reason="r", statement="Grace likes tea.")[1]
    assert res["ok"] and any("visibility raised to private" in n for n in res["notes"])
    rec = json.loads(w.log_lines()[-1])
    assert rec["visibility"] == "private" and len(w.log_lines()) == 1


def test_edit_does_not_overwrite_a_concurrent_change(ib, monkeypatch):
    """The write carries an `expect` of the fields it changes: a revision slipped in between our
    read and our write makes it a clean skip, not a lost update."""
    w = ib
    seed(w)
    w.start()
    real = w.lifecycle.revisions_store.append_revision

    def sneaky(key, changes, *a, **kw):
        monkeypatch.setattr(w.lifecycle.revisions_store, "append_revision", real)
        assert real(key, {"statement": "Someone else."}, "r", "cli", data_dir=w.env.data_dir).ok
        return real(key, changes, *a, **kw)
    monkeypatch.setattr(w.lifecycle.revisions_store, "append_revision", sneaky)
    res = w.lifecycle.edit_fact("p1", "mine", statement="Mine.", data_dir=w.env.data_dir, commit=False)
    assert not res.ok and res.outcome == "skipped" and "someone else" in res.message
    assert w.review.current_states(w.env.data_dir)["p1"]["statement"] == "Someone else."


# ---------------------------------------------------------------- make private (raise only)

def test_make_private_raises_visibility_only(ib):
    w = ib
    seed(w)
    srv = w.start()
    status, res = act(srv, action="make_private", ref="p1")
    assert status == 200 and items(res)[0][1] == "changed"
    rec = json.loads(w.log_lines()[-1])
    assert (rec["visibility"], rec["status"], rec["changed_via"]) == ("private", "pending", "inbox")
    assert items(act(srv, action="make_private", ref="p1")[1])[0][:2] == ("p1", "skipped")   # already private
    assert log_count(w) == 1


def test_a_normal_request_on_a_floored_fact_is_refused_with_the_rule(ib):
    w = ib
    w.seed(general=[pend("p1", "Call Grace.", visibility="private"), pend("p2", "Plain.", visibility="private")])
    srv = w.start(rules=w.privacy.Rules(keywords=("grace",)))
    status, res = act(srv, action="make_private", ref="p1", visibility="normal", reason="try")
    assert status == 409 and items(res)[0][1] == "refused"
    assert "keyword" in res["errors"][0] and "grace" in res["errors"][0]
    status, res = act(srv, action="make_private", ref="p2", visibility="normal", reason="try")   # not floored, still no lowering
    assert status == 409 and "only raises" in res["errors"][0]
    assert log_count(w) == 0


# ---------------------------------------------------------------- security

def test_binds_to_127_0_0_1_only(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert srv.server_address[0] == "127.0.0.1"
    src = open(os.path.join(REPO, "inbox.py")).read()
    assert "0.0.0.0" not in src and "--host" not in open(os.path.join(REPO, "cli_inbox.py")).read()


def test_non_loopback_peer_is_refused(ib):
    w = ib
    seed(w)
    srv = w.start()
    sent = []
    h = w.inbox.Handler.__new__(w.inbox.Handler)
    h.client_address, h.headers, h.server = ("10.1.2.3", 5555), {"Host": f"127.0.0.1:{srv.server_address[1]}"}, srv
    h._text = lambda status, msg: sent.append(status)
    assert h._guard() is False and sent == [403]
    h.client_address = ("127.0.0.1", 5555)
    h.headers = type("H", (), {"get": lambda self, k, d=None: {"Host": f"127.0.0.1:{srv.server_address[1]}"}.get(k, d)})()
    assert h._guard() is True


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1", "127.0.0.1:1", "attacker.com:%PORT%"])
def test_wrong_host_header_is_rejected_for_get_and_post(ib, host):
    w = ib
    seed(w)
    srv = w.start()
    host = host.replace("%PORT%", str(srv.server_address[1]))
    assert call(srv, host=host)[0] == 421
    assert call(srv, "POST", "/action", {"action": "approve", "ref": "p1"}, host=host)[0] == 421
    assert log_count(w) == 0


def test_missing_host_header_is_rejected(ib):
    import socket
    w = ib
    seed(w)
    srv = w.start()
    with socket.create_connection(("127.0.0.1", srv.server_address[1])) as s:
        s.sendall(b"GET / HTTP/1.0\r\n\r\n")
        assert s.recv(12).startswith(b"HTTP/1.0 421")


def test_localhost_host_header_is_accepted(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert call(srv, host=f"localhost:{srv.server_address[1]}")[0] == 200


@pytest.mark.parametrize("token", [False, "", "wrong", "x" * 5000])
def test_token_is_required_for_every_mutation(ib, token):
    w = ib
    seed(w)
    srv = w.start()
    for form in ({"action": "approve", "ref": "p1"}, {"action": "reject", "ref": "p1", "reason": "r"},
                 {"action": "edit_fact", "ref": "p1", "reason": "r", "statement": "New."},
                 {"action": "make_private", "ref": "p1"}, {"action": "approve_shown", "refs": "p1,p2"}):
        status, _, text = call(srv, "POST", "/action", form, token=token)
        assert status == 403, form
    assert log_count(w) == 0 and n_commits(w) == 1


def test_token_is_per_run_random_and_embedded_in_the_page(ib):
    w = ib
    seed(w)
    a = w.start()
    b = w.start(repo=False)
    assert a.token != b.token and len(a.token) >= 32
    assert f'value="{a.token}"' in call(a)[2] and a.token not in call(b)[2]
    assert call(b, "POST", "/action", {"action": "approve", "ref": "p1"}, token=a.token)[0] == 403


def test_get_cannot_mutate(ib):
    w = ib
    seed(w)
    srv = w.start()
    q = urllib.parse.urlencode({"action": "approve", "ref": "p1", "token": srv.token, "refs": "p1,p2"})
    assert call(srv, "GET", "/?" + q)[0] == 200
    assert call(srv, "GET", "/action?" + q)[0] == 404
    assert call(srv, "HEAD", "/?" + q)[0] == 200
    for m in ("PUT", "DELETE", "PATCH"):
        assert call(srv, m, "/action", {"action": "approve", "ref": "p1"})[0] == 405
    assert log_count(w) == 0 and n_commits(w) == 1


def test_post_needs_a_form_content_type_and_a_same_origin_origin(ib):
    w = ib
    seed(w)
    srv = w.start()
    body = json.dumps({"token": srv.token, "action": "approve", "ref": "p1"})
    assert call(srv, "POST", "/action", raw=body, headers={"Content-Type": "application/json"})[0] == 415
    assert call(srv, "POST", "/action", raw=body, headers={"Content-Type": "text/plain"})[0] == 415
    assert call(srv, "POST", "/action", {"action": "approve", "ref": "p1"}, headers={"Origin": "https://evil.example"})[0] == 403
    assert call(srv, "POST", "/action", {"action": "approve", "ref": "p1"}, headers={"Origin": "null"})[0] == 403
    assert call(srv, "POST", "/", {"action": "approve", "ref": "p1"})[0] == 404
    assert log_count(w) == 0
    own = f"http://127.0.0.1:{srv.server_address[1]}"
    assert call(srv, "POST", "/action", {"action": "approve", "ref": "p1"}, headers={"Origin": own})[0] == 200


def test_oversized_and_malformed_bodies(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert call(srv, "POST", "/action", raw="a=x", content_length=1_100_000, headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 413
    assert call(srv, "POST", "/action", raw="\xff=1".encode("latin-1"), headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 400
    assert call(srv, "POST", "/action", {"action": "launch-missiles"})[0] == 409
    assert call(srv, "POST", "/action", {})[0] == 409
    assert log_count(w) == 0


def test_no_cors_headers_and_options_is_refused(ib):
    w = ib
    seed(w)
    srv = w.start()
    for status, hdrs, _ in (call(srv, headers={"Origin": "https://evil.example"}),
                            call(srv, "POST", "/action", {"action": "approve", "ref": "zz"}, headers={"Origin": "https://evil.example"}),
                            call(srv, "OPTIONS", "/action", headers={"Origin": "https://evil.example",
                                                                     "Access-Control-Request-Method": "POST"})):
        assert not [h for h in hdrs if h.startswith("access-control")]
    assert call(srv, "OPTIONS", "/action")[0] == 405
    _, hdrs, _ = call(srv)
    assert "script-src" not in hdrs["content-security-policy"] and "default-src 'none'" in hdrs["content-security-policy"]
    assert hdrs["cache-control"] == "no-store" and hdrs["x-content-type-options"] == "nosniff"


EVIL = ['<script>alert(1)</script>', '"><img src=x onerror=alert(2)>', "'; DROP TABLE facts; --", "<b>bold</b> &amp; &lt;",
        "</textarea><script>alert(3)</script>"]


@pytest.mark.parametrize("evil", EVIL)
def test_markup_in_any_fact_field_is_rendered_inert(ib, evil):
    w = ib
    w.seed(general=[pend("p1", evil, source_quote=evil, notes=evil, trust_rationale=evil, captured_via="mcp",
                         session_id="s<script>x</script>", recheck_by="2027-01-01")])
    srv = w.start(repo=False)
    status, _, page = call(srv)
    assert status == 200
    assert "<script" not in page.lower() and "<img" not in page and "<b>bold" not in page
    assert "</textarea><script>" not in page
    assert evil in html.unescape(page)
    # the flash banner (echoing refs) is escaped too
    _, _, flash = call(srv, "POST", "/action", {"action": "approve", "ref": evil}, accept_json=False)
    assert "<script" not in flash.lower() and "<img" not in flash


def test_no_script_tag_anywhere_in_the_page(ib):
    w = ib
    seed(w)
    srv = w.start()
    assert "<script" not in call(srv)[2].lower()
    assert not re.search(r"<[^>]+\son\w+=", call(srv)[2])


# ---------------------------------------------------------------- CLI first (parity)

def _group():
    sys.modules.pop("knowledge", None)
    import knowledge
    return typer.main.get_command(knowledge.app)


def test_every_inbox_action_is_declared_registered_and_dispatched(ib):
    import cli_parity
    inbox = ib.inbox
    registry = dict(cli_parity.ACTIONS)
    assert set(inbox.PARITY_ACTIONS) <= set(registry)
    for name, path in inbox.PARITY_ACTIONS.items():
        assert tuple(registry[name]) == tuple(path), name
    assert cli_parity.declared_without_entry(inbox.PARITY_ACTIONS, cli_parity.ACTIONS) == []
    # every action the POST handler dispatches is declared (a button without a parity entry fails here)
    tree = ast.parse(open(os.path.join(REPO, "inbox.py")).read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_action")
    dispatched = {c.value for n in ast.walk(fn) if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id == "action"
                  for c in n.comparators if isinstance(c, ast.Constant)}
    assert dispatched and dispatched <= set(inbox.PARITY_ACTIONS), dispatched - set(inbox.PARITY_ACTIONS)
    assert dispatched == set(inbox.PARITY_ACTIONS) - {"list_pending"}      # list is the GET page
    # and every form on the page posts only declared actions
    w = ib
    seed(w)
    page = call(w.start())[2]
    assert set(re.findall(r'name="action" value="([^"]+)"', page)) <= set(inbox.PARITY_ACTIONS)


@pytest.mark.parametrize("action", ["list_pending", "approve", "reject", "make_private", "approve_shown", "edit_fact"])
def test_each_inbox_action_has_a_cli_command(ib, action):
    """DEPENDENCY: review-pending/approve/reject (#6) and set-visibility (#23) are built by other
    changes. Only the entry whose command is missing is skipped, never the whole test."""
    group = _group()
    path = ib.inbox.PARITY_ACTIONS[action]
    import cli_parity
    if cli_parity.resolve_command(group, path) is None and action != "edit_fact":
        pytest.skip(f"CLI command {' '.join(path)!r} for {action} is not built yet (other change)")
    assert cli_parity.resolve_command(group, path) is not None


def test_approve_all_flag_exists_once_the_command_does(ib):
    group = _group()
    if "approve" not in group.commands:
        pytest.skip("`approve` is not built yet (other change)")
    assert "--all" in {o for p in group.commands["approve"].params for o in p.opts}


def test_inbox_and_edit_commands_are_registered_in_knowledge(ib):
    group = _group()
    assert {"inbox", "edit"} <= set(group.commands)
    assert {"--port", "--no-open"} <= {o for p in group.commands["inbox"].params for o in p.opts}
    assert "--host" not in {o for p in group.commands["inbox"].params for o in p.opts}
    assert {"--statement", "--trust", "--trust-rationale", "--recheck-by", "--notes", "--reason"} <= \
        {o for p in group.commands["edit"].params for o in p.opts}


def test_knowledge_py_gets_exactly_one_line_for_this_feature():
    src = open(os.path.join(REPO, "knowledge.py")).read()
    assert src.count("cli_inbox") == 1


# ---------------------------------------------------------------- CLI: edit and inbox

def run_cli(w, *args):
    env = {**os.environ, **GIT_ENV, "KNOWLEDGE_PRIVATE_DIR": w.env.private}
    return subprocess.run([sys.executable, os.path.join(REPO, "knowledge.py"), *args], env=env, cwd=w.env.root,
                          capture_output=True, text=True, timeout=120)


def test_edit_cli_appends_a_revision_with_one_commit(ib):
    w = ib
    seed(w)
    w.start()  # builds the db file and the repo
    commits = n_commits(w)
    r = run_cli(w, "edit", "p1", "--statement", "CLI edit.", "--trust", "medium", "--notes", "n", "--reason", "why")
    assert r.returncode == 0, r.stderr
    rec = json.loads(w.log_lines()[-1])
    assert (rec["statement"], rec["trust_level"], rec["changed_via"], rec["change_reason"]) == ("CLI edit.", "medium", "cli", "why")
    assert n_commits(w) == commits + 1
    out = run_cli(w, "edit", "p1", "--notes", "m", "--reason", "again", "--json")
    assert out.returncode == 0 and json.loads(out.stdout)["outcome"] == "changed"


def test_edit_cli_by_fact_id_and_failures_exit_nonzero(ib):
    w = ib
    seed(w)
    w.start()
    fact_id = w.build().execute("SELECT id FROM facts WHERE source_key='p2'").fetchone()[0]
    assert run_cli(w, "edit", str(fact_id), "--notes", "by id", "--reason", "r").returncode == 0
    assert json.loads(w.log_lines()[-1])["source_key"] == "p2"
    n = log_count(w)
    for args in (["edit", "p1", "--reason", "r"], ["edit", "nope", "--notes", "x", "--reason", "r"],
                 ["edit", "p1", "--trust", "bogus", "--reason", "r"], ["edit", "p1", "--notes", "x", "--reason", " "],
                 ["edit", "999999", "--notes", "x", "--reason", "r"]):
        r = run_cli(w, *args)
        assert r.returncode != 0, args
    assert run_cli(w, "edit", "p1", "--notes", "x").returncode == 2        # --reason is required
    assert log_count(w) == n


def test_edit_cli_refuses_a_dirty_tree_and_digits_without_a_db(ib):
    w = ib
    seed(w)
    w.start(snapshot=True)
    open(os.path.join(w.env.data_dir, "stray"), "w").write("x")
    r = run_cli(w, "edit", "p1", "--notes", "x", "--reason", "r")
    assert r.returncode == 1 and "uncommitted changes" in r.stderr and log_count(w) == 0
    os.unlink(w.env.db)
    r = run_cli(w, "edit", "1", "--notes", "x", "--reason", "r")
    assert r.returncode == 1 and "database" in r.stderr


def test_inbox_command_serves_on_loopback_and_stops(ib):
    w = ib
    seed(w)
    w.start(repo=False)
    p = subprocess.Popen([sys.executable, os.path.join(REPO, "knowledge.py"), "inbox", "--port", "0", "--no-open"],
                         env={**os.environ, "KNOWLEDGE_PRIVATE_DIR": w.env.private}, cwd=w.env.root,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline()
        m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
        assert m, line + p.stderr.read() if p.poll() is not None else line
        c = http.client.HTTPConnection("127.0.0.1", int(m.group(1)), timeout=20)
        c.request("GET", "/")
        r = c.getresponse()
        assert r.status == 200 and b"Pending p1." in r.read()
    finally:
        p.terminate()
        p.wait(timeout=20)
