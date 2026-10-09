"""Local review inbox (#33): a small web page for the pending queue (#6), standard library only.

    knowledge.py inbox [--port N] [--no-open]      (the Typer command lives in cli_inbox.py)

LOCAL ONLY. The server binds 127.0.0.1 and nothing else (there is deliberately no --host option),
because it shows PRIVATE facts. It must never run in, or be reachable from, the remote normal-tier
process (#26), and a reverse proxy or port forward in front of it would defeat the checks below.

Checks on every request (a request that fails one gets a 4xx and touches nothing):
  * the peer address is loopback (403 otherwise);
  * the Host header is exactly 127.0.0.1:PORT or localhost:PORT (421 otherwise: DNS rebinding);
  * mutations are POST only, to /action, with the per-run random token embedded in the page
    (403 without it) and, when the browser sends an Origin header, an Origin on those hosts;
  * GET never changes anything (a GET carrying action parameters just renders the page);
  * no CORS headers are ever sent, and a CSP forbids scripts, so there is no JavaScript at all:
    every button is a plain HTML form;
  * every fact field is HTML-escaped (statements may contain markup or injection text).

CLI first (#34). Every button calls the same library function as a CLI command:
    list pending -> review.list_pending           (`review-pending`)
    approve      -> review.approve                (`approve`)
    reject       -> review.reject                 (`reject`)
    edit         -> lifecycle.edit_fact           (`edit`)
    make private -> lifecycle.set_visibility      (`set-visibility`, raise_only=True: the page never lowers)
    bulk approve -> review.approve over the keys shown on the page (`approve KEY...`; the CLI also
                    has `approve --all`, which the page deliberately does not use: the page lists a
                    db snapshot, and "all" would approve facts added after the last rebuild that
                    nobody has seen yet).
PARITY_ACTIONS below maps each action to its CLI command path.

Architecture (#36): this serving module imports only the libraries `review` and `lifecycle` plus
the standard library. It holds no write logic, no privacy rules and no git: the edit and
visibility writes are lifecycle functions, and the display state (pending overlay, the rule behind
a visibility) is review.pending_view. tach and import-linter enforce that.

The page lists the BUILT db (a snapshot, so it says to rebuild to see new pending facts), but each
fact's current state is read from the data files and revision log, which are the source of truth,
so a fact approved or rejected since the last rebuild is not offered again and a statement edited
since then is shown as edited. Every mutation is serialized in this process by a lock; the revision
log's own file lock and `expect` preconditions protect against the CLI and Claude sessions.

Library code (view_model) never prints or exits.
"""
import html
import ipaddress
import json
import re
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lifecycle
import review

# action -> CLI command path (words after `knowledge.py`). A dict, so iterating gives the names
# tests/test_cli_parity.py checks against cli_parity.ACTIONS.
PARITY_ACTIONS = {
    "list_pending": ("review-pending",),
    "approve": ("approve",),
    "reject": ("reject",),
    "edit_fact": ("edit",),
    "make_private": ("set-visibility",),
    "approve_shown": ("approve",),
}

DEFAULT_PORT = 8765
MAX_BODY = 1_000_000
VIA = "inbox"
BLANK_CLEARS = ("trust_rationale", "recheck_by", "notes")


# --------------------------------------------------------------------------- view model

def view_model(db_path, data_dir):
    """Everything the page shows: {facts, hidden_reviewed, missing_from_snapshot, error}. The
    overlay of current state on the db snapshot lives in review.pending_view (a library, so the
    rule behind each visibility is computed there, not here)."""
    return review.pending_view(db_path, data_dir)


# --------------------------------------------------------------------------- rendering

def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


CSS = """
body{font:15px/1.45 system-ui,sans-serif;margin:0 auto;max-width:60rem;padding:1rem 16px;color:#1a1a1a;background:#fafafa}
@media(prefers-color-scheme:dark){body{color:#e8e8e8;background:#1b1b1b}.fact,.banner,.flash{background:#262626!important}}
h1{font-size:1.3rem;margin:0 0 .5rem}.count{font-weight:700}
.banner,.flash{background:#fff;border:1px solid #8884;border-radius:6px;padding:.5rem .75rem;margin:.5rem 0}
.flash.bad{border-color:#c33}.flash.good{border-color:#2a7}
.fact{background:#fff;border:1px solid #8884;border-radius:8px;padding:.75rem;margin:.75rem 0}
.statement{font-size:1.05rem;white-space:pre-wrap;overflow-wrap:anywhere}
.meta{color:#777;font-size:.85rem;overflow-wrap:anywhere}.quote{border-left:3px solid #8886;margin:.4rem 0;padding-left:.6rem;white-space:pre-wrap;overflow-wrap:anywhere}
form{display:inline-block;margin:.25rem .25rem 0 0}details{margin-top:.4rem}
input[type=text],textarea,select{font:inherit;box-sizing:border-box;max-width:100%}textarea{width:100%;min-height:5rem}
label{display:block;margin:.3rem 0 .1rem;font-size:.85rem}button{font:inherit;padding:.25rem .7rem}
"""


def _form(token, action, fields, label, extra=""):
    hidden = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in fields.items())
    return (f'<form method="post" action="/action"><input type="hidden" name="token" value="{esc(token)}">'
            f'<input type="hidden" name="action" value="{esc(action)}">{hidden}{extra}<button>{esc(label)}</button></form>')


def _trust_options(selected):
    return "".join(f'<option{" selected" if t == selected else ""}>{esc(t)}</option>' for t in sorted(review.TRUST_LEVELS))


def render_fact(f, token):
    ref = f["ref"]
    prov = ", ".join(x for x in (f"via {f['captured_via']}" if f.get("captured_via") else "",
                                 f"session {f['session_id']}" if f.get("session_id") else "",
                                 f"captured {f['captured_at']}" if f.get("captured_at") else "") if x) or "no provenance recorded"
    quote = f'<blockquote class="quote">{esc(f["source_quote"])}</blockquote>' if f.get("source_quote") else ""
    reason_input = '<label>Reason <input type="text" name="reason" required></label>'
    edit = (
        f'<details><summary>Edit</summary><form method="post" action="/action">'
        f'<input type="hidden" name="token" value="{esc(token)}"><input type="hidden" name="action" value="edit_fact">'
        f'<input type="hidden" name="ref" value="{esc(ref)}">'
        f'<label>Statement<textarea name="statement">{esc(f["statement"])}</textarea></label>'
        f'<label>Trust level <select name="trust_level">{_trust_options(f["trust_level"])}</select></label>'
        f'<label>Trust rationale <input type="text" name="trust_rationale" value="{esc(f.get("trust_rationale"))}"></label>'
        f'<label>Recheck by (YYYY-MM-DD) <input type="text" name="recheck_by" value="{esc(f.get("recheck_by"))}"></label>'
        f'<label>Notes <input type="text" name="notes" value="{esc(f.get("notes"))}"></label>'
        f'{reason_input}<button>Save edit</button></form></details>')
    return (
        f'<div class="fact" id="fact-{esc(ref)}"><div class="statement">{esc(f["statement"])}</div>'
        f'<div class="meta">subject <b>{esc(f["subject"])}</b> &middot; trust {esc(f["trust_level"])} &middot; '
        f'added {esc(f.get("date_added"))} &middot; <b>{esc(f["visibility"])}</b></div>'
        f'<div class="meta">{esc(f["basis"])}</div><div class="meta">{esc(prov)}</div>{quote}'
        + _form(token, "approve", {"ref": ref}, "Approve")
        + _form(token, "make_private", {"ref": ref}, "Make private")
        + _form(token, "reject", {"ref": ref}, "Reject", '<input type="text" name="reason" placeholder="reason (required)" required>')
        + edit + "</div>")


def render_page(vm, token, flash=None):
    parts = []
    n = len(vm["facts"])
    parts.append(f'<h1>Pending facts: <span class="count">{n}</span> pending</h1>')
    parts.append('<div class="banner">This page lists the built database, a snapshot: rebuild '
                 '(<code>knowledge.py build</code>) to see new pending facts. Local only (127.0.0.1); it shows private facts.')
    if vm["hidden_reviewed"]:
        parts.append(f'<br>{vm["hidden_reviewed"]} fact(s) in the snapshot were already reviewed and are not shown.')
    if vm["missing_from_snapshot"]:
        parts.append(f'<br>{vm["missing_from_snapshot"]} more pending fact(s) are in the data files but not in this snapshot.')
    parts.append("</div>")
    if flash:
        parts.append(render_flash(flash))
    if vm["error"]:
        parts.append(f'<div class="flash bad">Cannot show the queue: {esc(vm["error"])}</div>')
    elif n == 0:
        parts.append("<p>Nothing is pending.</p>")
    else:
        refs = ",".join(f["ref"] for f in vm["facts"])
        parts.append(_form(token, "approve_shown", {"refs": refs}, f"Approve all {n} shown"))
        parts.extend(render_fact(f, token) for f in vm["facts"])
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pending facts</title>'
            f"<style>{CSS}</style></head><body>" + "".join(parts) + "</body></html>")


def render_flash(result):
    good = result.get("ok")
    lines = [f'<b>{esc(result.get("action"))}: {"done" if good else "not done"}</b>']
    for item in result.get("items", []):
        lines.append(f'{esc(item.get("ref"))}: {esc(item.get("outcome"))}' + (f' ({esc(item.get("message"))})' if item.get("message") else ""))
    lines.extend(esc(e) for e in result.get("errors", []))
    lines.extend(esc(n) for n in result.get("notes", []))
    if result.get("commit"):
        lines.append(f'committed {esc(result["commit"])}')
    return f'<div class="flash {"good" if good else "bad"}">' + "<br>".join(lines) + "</div>"


# --------------------------------------------------------------------------- actions

def _item_message(it):
    if it.outcome == "skipped" and it.reason:
        m = re.search(r"'(active|retracted|superseded|pending)'", it.reason)
        return f"already {review.status_word(m.group(1))}" if m else it.reason
    return it.reason or ""


def _from_review(action, res):
    return {"action": action, "ok": res.ok, "errors": res.errors, "notes": res.notes, "commit": res.commit,
            "items": [{"ref": i.ref, "outcome": i.outcome, "source_key": i.source_key, "message": _item_message(i)}
                      for i in res.items]}


def _from_revise(action, res):
    return {"action": action, "ok": res.ok, "errors": res.errors, "notes": res.notes, "commit": res.commit,
            "items": [{"ref": res.ref, "outcome": res.outcome, "source_key": res.source_key, "message": res.message}]}


def _from_visibility(action, ref, res):
    """A lifecycle.LifecycleResult (set_visibility) in the page's item shape."""
    if res.outcome == "visibility_set":
        outcome, message = "changed", "make-private: visibility"
    elif res.outcome == "unchanged":
        outcome = "skipped"
        m = re.search(r"status is '(active|retracted|superseded)'", res.reason or "")
        message = (f"already {review.status_word(m.group(1))}: nothing changed" if m
                   else "already private: nothing changed")
    elif res.outcome == "refused":
        outcome, message = "refused", "refused: " + (res.reason or "")
    else:
        outcome, message = "error", res.reason or "; ".join(res.errors)
    errors = list(res.errors)
    if res.outcome in ("refused", "unknown", "error") and not errors:
        errors.append(message)
    if res.commit_error:
        errors.append(res.commit_error)
    return {"action": action, "ok": res.ok, "errors": errors, "notes": res.notes, "commit": res.commit,
            "items": [{"ref": ref, "outcome": outcome, "source_key": res.source_key, "message": message}]}


def run_action(server, form):
    """Dispatch one POSTed action to the library; returns the structured result dict."""
    action = form.get("action", "")
    ref = form.get("ref", "").strip()
    common = dict(via=VIA, data_dir=server.data_dir, db=server.db_path)
    with server.mutation_lock:
        if action == "approve":
            return _from_review(action, review.approve([ref], reason=form.get("reason", "").strip() or "approved in inbox", **common))
        if action == "reject":
            return _from_review(action, review.reject([ref], form.get("reason", ""), **common))
        if action == "approve_shown":
            refs = [r for r in form.get("refs", "").split(",") if r.strip()]
            if not refs:
                return {"action": action, "ok": False, "errors": ["no facts to approve"], "notes": [], "commit": None, "items": []}
            return _from_review(action, review.approve(refs, reason="bulk approved in inbox", **common))
        if action == "edit_fact":
            res = lifecycle.edit_fact(ref, form.get("reason", ""), statement=form.get("statement"), trust_level=form.get("trust_level"),
                            trust_rationale=form.get("trust_rationale"), recheck_by=form.get("recheck_by"),
                            notes=form.get("notes"), expect_status="pending", **common)
            return _from_revise(action, res)
        if action == "make_private":
            res = lifecycle.set_visibility(ref, form.get("visibility", "private"),
                                           form.get("reason", "").strip() or "made private in inbox",
                                           expect_status="pending", raise_only=True, **common)
            return _from_visibility(action, ref, res)
    return {"action": action or "(none)", "ok": False, "errors": [f"unknown action {action!r}"], "notes": [], "commit": None, "items": []}


# --------------------------------------------------------------------------- server

class InboxServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, db_path, data_dir, port=0, token=None):
        self.db_path = db_path
        self.data_dir = data_dir
        self.token = token or secrets.token_urlsafe(32)
        self.mutation_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def allowed_hosts(self):
        port = self.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}


def make_server(db_path, data_dir=None, port=0, token=None):
    """An InboxServer bound to 127.0.0.1:port (0 = any free port). Not started."""
    data_dir = review.default_data_dir() if data_dir is None else data_dir
    return InboxServer(db_path, data_dir, port, token)


class Handler(BaseHTTPRequestHandler):
    server_version = "knowledge-inbox"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):  # no request logging: URLs and bodies stay out of terminals
        pass

    # -- plumbing
    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _text(self, status, message):
        self._send(status, f"{status} {message}\n", "text/plain; charset=utf-8")

    def _guard(self):
        """True when the request may proceed; otherwise a refusal was already sent."""
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            self._text(403, "local connections only")
            return False
        host = self.headers.get("Host", "")
        if host.lower() not in self.server.allowed_hosts:
            self._text(421, "unexpected Host header")
            return False
        return True

    def _wants_json(self):
        return "application/json" in self.headers.get("Accept", "")

    def _page(self, status=200, flash=None):
        vm = view_model(self.server.db_path, self.server.data_dir)
        self._send(status, render_page(vm, self.server.token, flash))

    # -- verbs
    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path != "/":
            self._text(404, "not found")
            return
        self._page()  # a GET never mutates, whatever its query string says

    do_HEAD = do_GET

    def do_POST(self):
        if not self._guard():
            return
        if urllib.parse.urlsplit(self.path).path != "/action":
            self._text(404, "not found")
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self.server.allowed_hosts}:
            self._text(403, "cross-origin request refused")
            return
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/x-www-form-urlencoded":
            self._text(415, "form posts only")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._text(411, "Content-Length required")
            return
        if length < 0 or length > MAX_BODY:
            self._text(413, "request too large")
            return
        raw = self.rfile.read(length)
        try:
            pairs = urllib.parse.parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=bool(raw))
        except (UnicodeDecodeError, ValueError):
            self._text(400, "bad form encoding")
            return
        form = {}
        for k, v in pairs:
            form.setdefault(k, v)
        if not secrets.compare_digest(form.get("token", "").encode("utf-8"), self.server.token.encode("utf-8")):
            self._text(403, "missing or wrong token")
            return
        result = run_action(self.server, form)
        status = 200 if result["ok"] else 409
        if self._wants_json():
            self._send(status, json.dumps(result, ensure_ascii=False), "application/json; charset=utf-8")
        else:
            self._page(status, result)

    def _method_not_allowed(self):
        if self._guard():
            self._text(405, "method not allowed")

    do_PUT = do_DELETE = do_PATCH = _method_not_allowed

    def do_OPTIONS(self):  # no CORS preflight support, on purpose
        self._method_not_allowed()


def serve(db_path, data_dir=None, port=DEFAULT_PORT, on_ready=None):
    """Serve until interrupted. `on_ready(url)` is called once the socket is bound."""
    server = make_server(db_path, data_dir, port)
    try:
        if on_ready:
            on_ready(f"http://127.0.0.1:{server.server_address[1]}/")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
