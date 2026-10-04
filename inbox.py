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
    edit         -> inbox.edit_fact               (`edit`, built here)
    make private -> inbox.set_visibility          (`set-visibility`)
    bulk approve -> review.approve over the keys shown on the page (`approve KEY...`; the CLI also
                    has `approve --all`, which the page deliberately does not use: the page lists a
                    db snapshot, and "all" would approve facts added after the last rebuild that
                    nobody has seen yet).
PARITY_ACTIONS below maps each action to its CLI command path. DEPENDENCY: `review-pending`,
`approve`, `reject` and `set-visibility` are built by other changes; until they exist,
tests/test_inbox.py skips only that entry. `inbox.set_visibility` goes straight to
revisions.append_revision (a raise-only subset of what `set-visibility` will do).

The page lists the BUILT db (a snapshot, so it says to rebuild to see new pending facts), but each
fact's current state is read from the data files and revision log, which are the source of truth,
so a fact approved or rejected since the last rebuild is not offered again and a statement edited
since then is shown as edited. Every mutation is serialized in this process by a lock; the revision
log's own file lock and `expect` preconditions protect against the CLI and Claude sessions.

Library code (edit_fact, set_visibility, view_model) never prints or exits.
"""
import datetime
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

import privacy
import review
import revisions
from private_git import PrivateGitError, commit_private_change, ensure_clean_tree, find_repo, is_detached

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


# --------------------------------------------------------------------------- library: edit

@dataclass
class ReviseResult:
    ok: bool
    outcome: str                       # 'changed' | 'skipped' | 'refused' | 'error'
    ref: str = ""
    source_key: Optional[str] = None
    message: str = ""                  # one line for a person
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    revision: Optional[dict] = None
    commit: Optional[str] = None
    commit_error: Optional[str] = None
    detached: bool = False

    def to_dict(self):
        return {"ok": self.ok, "outcome": self.outcome, "ref": self.ref, "source_key": self.source_key,
                "message": self.message, "errors": self.errors, "notes": self.notes, "revision": self.revision,
                "commit": self.commit, "commit_error": self.commit_error}


def _fail(ref, message, outcome="error", source_key=None):
    return ReviseResult(False, outcome, str(ref), source_key, message, errors=[message])


def _db_context(rules, db):
    """`rules` plus the subject tree from the db (read-only), as `privacy check` does."""
    if db is None:
        return rules
    try:
        with revisions._connection(db) as con:
            rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
    except Exception:  # noqa: BLE001 - a missing/odd db only means less context, never a crash
        return rules
    parents = {n: p for n, p in rows}
    return rules.with_context(parents=parents, known_subjects=set(parents))


def _load_rules(data_dir, db):
    return _db_context(privacy.load_rules(privacy.rules_path(data_dir)), db)


def _subjects(data_dir):
    return {e["key"]: e["entry"].get("subject") for e in revisions.load_entries(data_dir)}


def _validate_edit(changes):
    """Normalize and validate requested edits -> (changes, errors). A blank rationale / recheck /
    notes clears the field; a blank statement is refused."""
    errors, out = [], {}
    for name, value in changes.items():
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{name} must be text")
            continue
        if name == "statement":
            if not value.strip():
                errors.append("statement must not be blank")
                continue
            out[name] = value.strip()
        elif name == "trust_level":
            if value not in revisions.VALID_TRUST:
                errors.append(f"trust level {value!r} must be one of {sorted(revisions.VALID_TRUST)}")
                continue
            out[name] = value
        elif name == "recheck_by":
            text = value.strip()
            if text:
                try:
                    datetime.date.fromisoformat(text)
                except ValueError:
                    errors.append(f"recheck-by {value!r} must be a date as YYYY-MM-DD")
                    continue
            out[name] = text or None
        else:
            out[name] = value.strip() or None
    return out, errors


def _revise(ref, changes, reason, via, session_id, data_dir, commit, allow_dirty, db, expect_status, verb, adjust=None):
    """Shared by edit_fact and set_visibility: resolve, check, one locked write, one commit.
    `adjust(key, current, changes) -> (changes, ReviseResult | None)` may add fields or refuse."""
    ref = str(ref)
    if not isinstance(reason, str) or not reason.strip():
        return _fail(ref, "reason is required")
    data_dir = revisions.default_data_dir() if data_dir is None else data_dir
    try:
        states = review.current_states(data_dir)
    except revisions.RevisionError as e:
        return _fail(ref, str(e))
    try:
        key, problem = review._resolve(ref, states, db)
    except sqlite3.Error as e:
        return _fail(ref, f"could not read the database to resolve fact id {ref}: {e}")
    if problem:
        return _fail(ref, problem, "error")
    current = states[key]
    if expect_status is not None and current["status"] != expect_status:
        return ReviseResult(True, "skipped", ref, key, f"already {_status_word(current['status'])}: nothing changed")
    if current["status"] == "retracted":
        return ReviseResult(False, "skipped", ref, key, "already retracted: not edited")
    notes = []
    if adjust is not None:
        try:
            changes, early = adjust(key, current, dict(changes))
        except privacy.PrivacyRulesError as e:
            return _fail(ref, str(e), source_key=key)
        if early is not None:
            early.ref, early.source_key = ref, key
            return early
    if all(current[k] == v for k, v in changes.items()):
        return _fail(ref, "no field would change", source_key=key)

    repo, log_path = None, os.path.join(data_dir, revisions.REVISIONS_FILENAME)
    if commit:
        try:
            repo = find_repo(data_dir)
            if repo is None:
                notes.append(f"{data_dir} is not inside a git repository; the change will not be committed.")
            elif not allow_dirty:
                ensure_clean_tree(repo)
        except PrivateGitError as e:
            return _fail(ref, str(e), source_key=key)

    expect = {"status": expect_status or current["status"], **{k: current[k] for k in changes}}
    res = revisions.append_revision(key, changes, reason, via, session_id, data_dir=data_dir, expect=expect)
    if not res.ok:
        if any(e.startswith("precondition failed") for e in res.errors):
            return ReviseResult(False, "skipped", ref, key,
                                "changed by someone else since it was loaded; reload and try again", errors=res.errors)
        return _fail(ref, "; ".join(res.errors), source_key=key)
    out = ReviseResult(True, "changed", ref, key, f"{verb}: {', '.join(changes)}", notes=notes, revision=res.revision)
    if repo is not None:
        try:
            out.commit = commit_private_change([log_path], f"{verb}: {key} ({', '.join(changes)})", repo)
            out.detached = is_detached(repo)
        except PrivateGitError as e:
            out.ok = False
            out.commit_error = f"the revision IS in {log_path} but is NOT committed: {e}"
            out.errors.append(out.commit_error)
    return out


def _status_word(status):
    return {"active": "approved", "retracted": "rejected", "pending": "pending"}.get(status, status)


def edit_fact(ref, reason, statement=None, trust_level=None, trust_rationale=None, recheck_by=None, notes=None,
              via="cli", session_id=None, data_dir=None, commit=True, allow_dirty=False, db=None,
              expect_status=None):
    """Append one revision changing the given fields (None = leave alone; "" clears trust_rationale,
    recheck_by, notes). The subject is immutable and the visibility is never lowered: if the new
    statement trips a privacy rule the revision also raises visibility to private. `expect_status`
    (the inbox passes 'pending') makes a fact reviewed elsewhere since it was listed a clean
    skip. The write carries an `expect` of every field it changes as it was read, so a concurrent
    edit is never silently overwritten. ONE commit in the data repo, like review.approve."""
    changes, errors = _validate_edit({"statement": statement, "trust_level": trust_level,
                                      "trust_rationale": trust_rationale, "recheck_by": recheck_by, "notes": notes})
    if errors:
        return _fail(ref, "; ".join(errors))
    if not changes:
        return _fail(ref, "nothing to edit: give at least one field")

    def adjust(key, current, ch):
        if "statement" not in ch:
            return ch, None
        subject = _subjects(revisions.default_data_dir() if data_dir is None else data_dir).get(key)
        rules = _load_rules(data_dir, db)
        res = privacy.resolve_visibility(subject, ch["statement"], current["visibility"], rules)
        if res.visibility == "private" and current["visibility"] != "private":
            ch["visibility"] = "private"
            ch_note = f"visibility raised to private: {res.explain()}"
            adjust.notes.append(ch_note)
        return ch, None
    adjust.notes = []
    out = _revise(ref, changes, reason, via, session_id, data_dir, commit, allow_dirty, db, expect_status, "edit", adjust)
    out.notes.extend(adjust.notes)
    return out


# --------------------------------------------------------------------------- library: visibility

def set_visibility(ref, visibility, reason, via="cli", session_id=None, data_dir=None, commit=True,
                   allow_dirty=False, db=None, expect_status=None):
    """Raise-only (#31): 'private' appends a visibility revision; 'normal' is always refused here,
    naming the privacy rule when one floors the fact. Lowering is the job of `set-visibility`."""
    if visibility not in privacy.VALID_VISIBILITY:
        return _fail(ref, f"visibility {visibility!r} must be one of {list(privacy.VALID_VISIBILITY)}")

    def adjust(key, current, ch):
        if visibility == "private":
            if current["visibility"] == "private":
                return ch, ReviseResult(True, "skipped", message="already private: nothing changed")
            return ch, None
        rules = _load_rules(data_dir, db)
        subject = _subjects(revisions.default_data_dir() if data_dir is None else data_dir).get(key)
        res = privacy.resolve_visibility(subject, current["statement"], "normal", rules)
        if res.visibility == "private":
            msg = f"refused: cannot be made normal; {res.explain()}"
        else:
            msg = "refused: the inbox only raises visibility to private; lowering is done with `set-visibility`"
        return ch, ReviseResult(False, "refused", message=msg, errors=[msg])

    return _revise(ref, {"visibility": "private"}, reason, via, session_id, data_dir, commit, allow_dirty, db,
                   expect_status, "make-private", adjust)


# --------------------------------------------------------------------------- view model

def visibility_basis(subject, statement, visibility, rules):
    """Which rule is behind a stored visibility, as one line."""
    res = privacy.check(subject, statement, rules, requested="normal")
    if res.visibility == "private":
        return "private by rule: " + "; ".join(str(r) for r in res.raised_by)
    if visibility == "private":
        return "private by request or default (no privacy rule applies)"
    return "normal: no privacy rule applies"


def view_model(db_path, data_dir):
    """Everything the page shows: {facts, snapshot_pending, hidden_reviewed, missing_from_snapshot, error}.
    Pending rows come from the built db; each is overlaid with the current state from the data
    files and revision log, and dropped when that says it is no longer pending."""
    vm = {"facts": [], "hidden_reviewed": 0, "missing_from_snapshot": 0, "error": None}
    if not os.path.exists(db_path):
        vm["error"] = f"{db_path} does not exist: run `knowledge.py build` first"
        return vm
    try:
        rows = review.list_pending(db_path)
        states = review.current_states(data_dir)
        rules = _db_context(privacy.load_rules(privacy.rules_path(data_dir)), db_path)
    except (revisions.RevisionError, privacy.PrivacyRulesError) as e:
        vm["error"] = str(e)
        return vm
    except Exception as e:  # noqa: BLE001 - e.g. sqlite3.Error on a half-built db: show it, don't crash
        vm["error"] = f"could not read the database: {e}"
        return vm
    listed = set()
    for r in rows:
        key = r["source_key"]
        listed.add(key)
        cur = states.get(key)
        if cur is not None and cur["status"] != "pending":
            vm["hidden_reviewed"] += 1
            continue
        fact = dict(r)
        if cur is not None:
            fact.update(statement=cur["statement"], trust_level=cur["trust_level"], visibility=cur["visibility"],
                        trust_rationale=cur["trust_rationale"], recheck_by=cur["recheck_by"], notes=cur["notes"])
        else:
            fact.update(trust_rationale=None, recheck_by=None, notes=None)
        fact["ref"] = str(key if key else r["id"])
        fact["basis"] = visibility_basis(r["subject"], fact["statement"], fact["visibility"], rules)
        vm["facts"].append(fact)
    vm["missing_from_snapshot"] = sum(1 for k, s in states.items() if s["status"] == "pending" and k not in listed)
    return vm


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
    return "".join(f'<option{" selected" if t == selected else ""}>{esc(t)}</option>' for t in sorted(revisions.VALID_TRUST))


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
        return f"already {_status_word(m.group(1))}" if m else it.reason
    return it.reason or ""


def _from_review(action, res):
    return {"action": action, "ok": res.ok, "errors": res.errors, "notes": res.notes, "commit": res.commit,
            "items": [{"ref": i.ref, "outcome": i.outcome, "source_key": i.source_key, "message": _item_message(i)}
                      for i in res.items]}


def _from_revise(action, res):
    return {"action": action, "ok": res.ok, "errors": res.errors, "notes": res.notes, "commit": res.commit,
            "items": [{"ref": res.ref, "outcome": res.outcome, "source_key": res.source_key, "message": res.message}]}


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
            res = edit_fact(ref, form.get("reason", ""), statement=form.get("statement"), trust_level=form.get("trust_level"),
                            trust_rationale=form.get("trust_rationale"), recheck_by=form.get("recheck_by"),
                            notes=form.get("notes"), expect_status="pending", **common)
            return _from_revise(action, res)
        if action == "make_private":
            res = set_visibility(ref, form.get("visibility", "private"), form.get("reason", "").strip() or "made private in inbox",
                                 expect_status="pending", **common)
            return _from_revise(action, res)
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
    data_dir = revisions.default_data_dir() if data_dir is None else data_dir
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
