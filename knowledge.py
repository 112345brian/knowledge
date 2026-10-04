#!/usr/bin/env python3
"""Single entry point for this repo (Typer CLI; run `uv sync` once, then `uv run python knowledge.py --help`).

    knowledge.py build [--check]
    knowledge.py add-fact "statement" --subject x --trust medium ...
    knowledge.py clean-concerts
    knowledge.py search "some terms" [--subject x] [--trust high] [--personal-only|--not-personal] [--status S] [--include-pending] [--limit N] [--json]
    knowledge.py show <fact_id> [--as-of DATE] [--json]
    knowledge.py history <fact_id|source_key> [--json]
    knowledge.py audit-claims [--json]
    knowledge.py privacy check "statement" --subject s [--requested normal|private] [--json]
    knowledge.py privacy rules [--json]
    knowledge.py privacy tag|untag <subject> / add-keyword|remove-keyword <word>  [--allow-dirty] [--dry-run]
    knowledge.py subjects [--include-pending] [--json]
    knowledge.py facts [--subject x] [--trust high] [--status active|pending|...] [--include-pending] [--personal-only|--not-personal] [--limit N] [--json]
    knowledge.py review-pending [--json]
    knowledge.py approve REF... | --all [--reason TEXT] [--allow-dirty] [--json]
    knowledge.py reject REF... --reason TEXT [--allow-dirty] [--json]

Convention (issue #34), for every later command: the library function comes
first (pure, importable, returns data, never prints or exits), the Typer
command second (a thin printer, with `--json` on every read command), the MCP
tool / inbox handler third. `cli_parity.py` registers each action's CLI
command and tests/test_cli_parity.py fails when a registered command is
missing or a future MCP tool / inbox action has no registry entry.

`build`, `add-fact`, and `clean-concerts` are thin dispatches to the existing
standalone scripts (build.py, add_fact.py, clean_concerts_csv.py) -- those
still run fine on their own, without typer; this just gives one name to
remember. `search`, `show`, `subjects`, and `facts` query knowledge.db.
"""
import enum
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
from typing import List, Optional

import typer

import add_fact
import claims_audit
import private_git
import privacy
import review
import revisions
from paths import KNOWLEDGE_DB_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")


class Trust(str, enum.Enum):
    verified = "verified"
    high = "high"
    medium = "medium"
    low = "low"
    unverified = "unverified"
    disputed = "disputed"


class Status(str, enum.Enum):
    pending = "pending"
    active = "active"
    superseded = "superseded"
    retracted = "retracted"


class DatabaseNotFound(FileNotFoundError):
    """knowledge.db doesn't exist yet (it is built, never hand-created)."""


def connect(db_path=None):
    """Open the knowledge db READ-ONLY. Raises DatabaseNotFound if it is missing.

    Everything in this module only reads; writes go through build.py, which
    makes its own connection."""
    path = os.path.abspath(db_path or DB_PATH)
    if not os.path.exists(path):
        raise DatabaseNotFound(f"{path} doesn't exist -- run `python3 knowledge.py build` first")
    con = sqlite3.connect(pathlib.Path(path).as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ---- query functions: take a connection, return plain dicts, never print or exit ----

def _visible_statuses(include_pending):
    """Statuses shown when the caller names none: active only (#6), plus pending on request.
    Superseded and retracted facts need an explicit status filter. Same rule as modes.py."""
    return ("active", "pending") if include_pending else ("active",)


def _filters(sql, params, subject=None, trust=None, status=None, personal=None, include_pending=False):
    """Append the shared fact filters. `personal` is True / False / None (no filter).
    An explicit `status` wins and `include_pending` is then ignored; without one only
    active facts (and pending ones when `include_pending`) match."""
    if subject:
        sql += " AND sub.name = ?"
        params.append(subject)
    if trust:
        sql += " AND f.trust_level = ?"
        params.append(trust)
    if status:
        sql += " AND f.status = ?"
        params.append(status)
    else:
        statuses = _visible_statuses(include_pending)
        sql += f" AND f.status IN ({','.join('?' for _ in statuses)})"
        params.extend(statuses)
    if personal is True:
        sql += " AND f.is_personal = 1"
    elif personal is False:
        sql += " AND f.is_personal = 0"
    return sql


def search_facts(con, terms, subject=None, trust=None, personal=None, limit=20, status=None, include_pending=False):
    """Full-text search, best match first; active facts unless `status` / `include_pending` say otherwise. Raises sqlite3.OperationalError on FTS syntax errors."""
    sql = """
        SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.statement
        FROM facts_fts
        JOIN facts f ON f.id = facts_fts.rowid
        JOIN subjects sub ON sub.id = f.subject_id
        WHERE facts_fts MATCH ?
    """
    params = [terms]
    sql = _filters(sql, params, subject=subject, trust=trust, status=status, personal=personal,
                   include_pending=include_pending)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def list_facts(con, subject=None, trust=None, status=None, personal=None, limit=50, include_pending=False):
    """Facts by id; active only unless `status` names one or `include_pending` adds pending."""
    sql = """SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.statement
             FROM facts f JOIN subjects sub ON sub.id = f.subject_id WHERE 1=1"""
    params = []
    sql = _filters(sql, params, subject=subject, trust=trust, status=status, personal=personal,
                   include_pending=include_pending)
    sql += " ORDER BY f.id LIMIT ?"
    params.append(limit)
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_fact(con, fact_id):
    """One fact (all columns, plus subject and origin_path) with a `sources` list of
    {name, locator} dicts; None if there is no such fact. Deliberately NOT status-filtered
    (#6): asking for an id by name shows it whatever its status, pending included."""
    f = con.execute(
        """SELECT f.*, sub.name AS subject, vf.path AS origin_path
           FROM facts f
           JOIN subjects sub ON sub.id = f.subject_id
           LEFT JOIN vault_files vf ON vf.id = f.origin_file_id
           WHERE f.id = ?""",
        (fact_id,),
    ).fetchone()
    if not f:
        return None
    out = dict(f)
    out["sources"] = [dict(s) for s in con.execute(
        """SELECT s.name, fs.locator
           FROM fact_sources fs JOIN sources s ON s.id = fs.source_id
           WHERE fs.fact_id = ?""",
        (f["id"],),
    ).fetchall()]
    return out


def list_subjects(con, include_pending=False):
    """Every subject with `n_facts` = its active facts (plus pending ones when `include_pending`).
    Subjects with no counted facts are still listed, with 0."""
    statuses = _visible_statuses(include_pending)
    return [dict(r) for r in con.execute(
        f"""SELECT s.name, s.domain, p.name AS parent, COUNT(f.id) AS n_facts
            FROM subjects s
            LEFT JOIN subjects p ON p.id = s.parent_id
            LEFT JOIN facts f ON f.subject_id = s.id AND f.status IN ({','.join('?' for _ in statuses)})
            GROUP BY s.id
            ORDER BY s.domain, COALESCE(p.name, s.name), s.name""",
        statuses).fetchall()]


# ---- thin CLI printers (Typer) ----

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False, rich_markup_mode=None)
import cli_facts_batch; cli_facts_batch.register(app)  # `add-facts` (#32)


def _personal(personal_only, not_personal):
    if personal_only and not_personal:
        raise typer.BadParameter("--personal-only and --not-personal are mutually exclusive")
    return True if personal_only else (False if not_personal else None)


def _fail(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def _query(fn, *a, **kw):
    """Open the db, run one library function, close. A missing db is a one-line error, exit 1."""
    try:
        con = connect()
    except DatabaseNotFound as e:
        _fail(e)
    try:
        return fn(con, *a, **kw)
    finally:
        con.close()


def _emit_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_fact_lines(rows):
    if not rows:
        print("No matches.")
        return
    for r in rows:
        flag = "" if r["status"] == "active" else f" [{r['status']}]"
        print(f"#{r['id']:<5} [{r['subject']}] ({r['trust_level']}){flag}  {r['statement']}")


JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead of text.")
ALLOW_DIRTY_OPT_REVIEW = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on the data repo (deliberate batch edits only); the commit still contains only fact_revisions.jsonl.")
INCLUDE_PENDING_OPT = typer.Option(False, "--include-pending", help="Also show pending (unreviewed) facts; by default only active facts are listed.")


@app.command("build", help="Rebuild knowledge.db from schema.sql + scripts + data/.")
def cmd_build(check: bool = typer.Option(False, "--check", help="Build into a throwaway file and report counts; live DB untouched.")):
    cmd = [sys.executable, os.path.join(HERE, "build.py")]
    if check:
        cmd.append("--check")
    raise typer.Exit(subprocess.call(cmd))


@app.command("add-fact", help="Append an ad hoc fact to data/general_facts.json. Every argument is forwarded to add_fact.py (see add_fact.py --help).",
             context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []})
def cmd_add_fact(ctx: typer.Context):
    raise typer.Exit(subprocess.call([sys.executable, os.path.join(HERE, "add_fact.py")] + list(ctx.args)))


@app.command("clean-concerts", help="Clean concerts.csv in place (dedupes rows).")
def cmd_clean_concerts():
    raise typer.Exit(subprocess.call([sys.executable, os.path.join(HERE, "clean_concerts_csv.py")]))


@app.command("search", help="Full-text search over facts (statement/trust_rationale/notes).")
def cmd_search(
    terms: str,
    subject: Optional[str] = None,
    trust: Optional[Trust] = None,
    limit: int = 20,
    personal_only: bool = typer.Option(False, "--personal-only"),
    not_personal: bool = typer.Option(False, "--not-personal"),
    status: Optional[Status] = typer.Option(None, "--status", help="Only facts with this status (overrides the active-only default and --include-pending)."),
    include_pending: bool = INCLUDE_PENDING_OPT,
    as_json: bool = JSON_OPT,
):
    personal = _personal(personal_only, not_personal)
    try:
        rows = _query(search_facts, terms, subject=subject, trust=trust.value if trust else None,
                      personal=personal, limit=limit, status=status.value if status else None,
                      include_pending=include_pending)
    except sqlite3.OperationalError as e:
        _fail(f"search failed: {e}")
    _emit_json(rows) if as_json else _print_fact_lines(rows)


def fact_as_of(con, fact_id, as_of):
    """('ok', revision) | ('no-fact', None) | ('no-history', None) | ('not-yet', None).
    Raises ValueError for a bad `as_of` (from revisions.get_fact_as_of)."""
    rev = revisions.get_fact_as_of(con, fact_id, as_of)
    if rev is not None:
        return "ok", rev
    if get_fact(con, fact_id) is None:
        return "no-fact", None
    return ("not-yet", None) if revisions.get_history(con, fact_id) else ("no-history", None)


def _print_revision_state(r):
    print(f"\n{r['statement']}\n")
    for label, key in (("Trust rationale", "trust_rationale"), ("Notes", "notes"), ("Superseded by", "superseded_by"),
                       ("Freshness", "freshness")):
        if r[key]:
            print(f"{label}: {r[key]}")
    if r["recheck_by"]:
        print(f"Recheck by: {r['recheck_by']}" + (f"  ({r['recheck_rationale']})" if r["recheck_rationale"] else ""))


def _show_as_of(fact_id, as_of, as_json):
    try:
        kind, r = _query(fact_as_of, fact_id, as_of)
    except ValueError as e:
        _fail(e)
    if kind == "no-fact":
        _fail(f"no fact with id {fact_id}")
    if kind == "no-history":
        _fail(f"fact {fact_id} has no revision history (rebuild, or it predates revisions)")
    if kind == "not-yet":
        _fail(f"fact {fact_id} did not exist yet at {as_of}")
    if as_json:
        _emit_json(r)
        return
    via = f" via {r['changed_via']}" if r["changed_via"] else ""
    print(f"Fact #{r['fact_id']}  [{r['subject']}]  as of {as_of}  (revision {r['revision']}, changed {r['changed_at']}{via})")
    print(f"trust={r['trust_level']}  visibility={r['visibility']}  status={r['status']}")
    if r["change_reason"]:
        print(f"Reason: {r['change_reason']}")
    _print_revision_state(r)


@app.command("show", help="Show one fact in full, with its sources. --as-of DATE shows it as it stood then (end of that day, UTC; or an ISO timestamp).")
def cmd_show(fact_id: int, as_json: bool = JSON_OPT,
             as_of: Optional[str] = typer.Option(None, "--as-of", help="YYYY-MM-DD (end of that day, UTC) or an ISO-8601 timestamp.")):
    if as_of is not None:
        return _show_as_of(fact_id, as_of, as_json)
    f = _query(get_fact, fact_id)
    if not f:
        _fail(f"no fact with id {fact_id}")
    if as_json:
        _emit_json(f)
        return

    print(f"Fact #{f['id']}  [{f['subject']}]  trust={f['trust_level']}  personal={bool(f['is_personal'])}  visibility={f['visibility']}  status={f['status']}")
    print(f"\n{f['statement']}\n")
    if f["trust_rationale"]:
        print(f"Trust rationale: {f['trust_rationale']}")
    if f["notes"]:
        print(f"Notes: {f['notes']}")
    if f["origin_path"]:
        print(f"Origin: {f['origin_path']}")
    if f["recheck_by"]:
        print(f"Recheck by: {f['recheck_by']}" + (f"  ({f['recheck_rationale']})" if f["recheck_rationale"] else ""))
    if f["source_key"]:
        print(f"Source key: {f['source_key']}")
    if f["captured_via"] or f["session_id"] or f["captured_at"]:
        parts = [f"{label}={f[col]}" for label, col in (("via", "captured_via"), ("session", "session_id"), ("at", "captured_at")) if f[col]]
        print("Captured: " + "  ".join(parts))
    if f["source_quote"]:
        print(f"Source quote: {f['source_quote']}")
    if f["sources"]:
        print("\nSources:")
        for s in f["sources"]:
            loc = f" ({s['locator']})" if s["locator"] else ""
            print(f"  - {s['name']}{loc}")


def _revision_diff(prev, cur):
    return [f"{k}: {prev[k]!r} -> {cur[k]!r}" for k in revisions.MUTABLE_FIELDS if prev[k] != cur[k]]


@app.command("history", help="Every revision of a fact, oldest first. REF is a fact id (digits) or a source_key.")
def cmd_history(ref: str, as_json: bool = JSON_OPT):
    ref = ref.strip()
    if not ref:
        _fail("give a fact id or a source_key")
    key = int(ref) if ref.isascii() and ref.isdigit() else ref
    rows = _query(revisions.get_history, key)
    if not rows:
        _fail(f"no revision history for {ref!r} (unknown fact id or source_key, or the fact has no revisions)")
    if as_json:
        _emit_json(rows)
        return
    first = rows[0]
    n = len(rows)
    print(f"Fact #{first['fact_id']}  [{first['subject']}]  source_key={first['source_key']}  ({n} revision{'' if n == 1 else 's'})")
    prev = None
    for r in rows:
        extra = (f"  via={r['changed_via']}" if r["changed_via"] else "") + (f"  session={r['session_id']}" if r["session_id"] else "")
        print(f"\nrev {r['revision']}  {r['changed_at']}{extra}")
        if r["change_reason"]:
            print(f"  reason: {r['change_reason']}")
        if prev is None:
            for k in revisions.MUTABLE_FIELDS:
                if r[k] not in (None, ""):
                    print(f"  {k}: {r[k]}")
        else:
            diff = _revision_diff(prev, r)
            for d in diff or ["(no field changed)"]:
                print(f"  {d}")
        prev = r


@app.command("audit-claims", help="List claims whose premises (cited facts) are superseded, retracted or past recheck_by. Exit 1 when any are found.")
def cmd_audit_claims(as_json: bool = JSON_OPT):
    def run(con):
        return claims_audit.audit_claims(con), claims_audit.unparseable_rechecks(con)
    try:
        rows, unparsed = _query(run)
    except sqlite3.OperationalError as e:
        _fail(f"audit failed: {e} (rebuild knowledge.db with the current schema)")
    if as_json:
        _emit_json({"stale_premises": rows, "unparseable_rechecks": unparsed})
    elif not rows:
        print("No stale premises.")
    else:
        for r in rows:
            kind = f" ({r['inference_type']})" if r["inference_type"] else ""
            print(f"claim #{r['claim_id']}{kind}: {r['claim_statement']}")
            line = f"  fact #{r['fact_id']} {r['reason']}"
            if r["reason"] == "past_recheck_by":
                line += f" ({r['recheck_by']})"
            elif r["superseded_by_fact_id"]:
                line += f" (by fact #{r['superseded_by_fact_id']})"
            print(f"{line}: {r['fact_statement']}")
    if unparsed and not as_json:
        ids = ", ".join(f"#{u['fact_id']} ({u['recheck_by']!r})" for u in unparsed)
        print(f"note: {len(unparsed)} cited fact(s) have a recheck_by that is not an ISO date, so the audit cannot judge them: {ids}", file=sys.stderr)
    if rows:
        raise typer.Exit(1)


@app.command("subjects", help="List subjects (indented under parent) with counts of their active facts.")
def cmd_subjects(include_pending: bool = INCLUDE_PENDING_OPT, as_json: bool = JSON_OPT):
    rows = _query(list_subjects, include_pending=include_pending)
    if as_json:
        _emit_json(rows)
        return
    for r in rows:
        indent = "  " if r["parent"] else ""
        print(f"{indent}{r['name']:<35} ({r['domain']}, {r['n_facts']} facts)")


@app.command("facts", help="List/filter facts without full-text search. Active facts by default; --include-pending adds pending ones, --status X shows only status X.")
def cmd_facts(
    subject: Optional[str] = None,
    trust: Optional[Trust] = None,
    status: Optional[Status] = None,
    limit: int = 50,
    personal_only: bool = typer.Option(False, "--personal-only"),
    not_personal: bool = typer.Option(False, "--not-personal"),
    include_pending: bool = INCLUDE_PENDING_OPT,
    as_json: bool = JSON_OPT,
):
    rows = _query(list_facts, subject=subject, trust=trust.value if trust else None,
                  status=status.value if status else None, include_pending=include_pending,
                  personal=_personal(personal_only, not_personal), limit=limit)
    _emit_json(rows) if as_json else _print_fact_lines(rows)


# ---- review of pending facts (#6): `review-pending`, `approve`, `reject` ----

def _review_json(res):
    return {"ok": res.ok, "items": [dict(ref=i.ref, outcome=i.outcome, source_key=i.source_key, reason=i.reason,
                                         revision=i.revision) for i in res.items],
            "errors": res.errors, "notes": res.notes, "commit": res.commit,
            "commit_error": res.commit_error, "detached": res.detached}


def _review_exit_code(res):
    """3 when revisions were written but not committed (the state that needs attention);
    else 1 for a batch error or any unknown/error item; else 0. Skipped items are fine."""
    if res.commit_error is not None:
        return 3
    return 0 if res.ok else 1


def _report_review(res, as_json):
    if as_json:
        _emit_json(_review_json(res))
    else:
        for e in res.errors:
            print(f"error: {e}", file=sys.stderr)
        for i in res.items:
            if i.outcome in ("approved", "rejected"):
                print(f"{i.outcome} {i.ref}" + (f" ({i.source_key})" if i.source_key and i.source_key != i.ref else ""))
            else:
                print(f"{i.outcome} {i.ref}: {i.reason}", file=sys.stderr if i.outcome in ("unknown", "error") else sys.stdout)
        if not res.items and not res.errors:
            print("Nothing to do.")
        for n in res.notes:
            print(f"note: {n}", file=sys.stderr)
        if res.commit:
            print(f"Committed {res.commit}")
        if res.commit_error:
            print(f"error: {res.commit_error}", file=sys.stderr)
        if res.detached:
            print("warning: the data repo has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
        if res.changed:
            print("note: knowledge.db is not rebuilt yet; run `knowledge.py build` to see the change.", file=sys.stderr)
    code = _review_exit_code(res)
    if code:
        raise typer.Exit(code)


def _review_db():
    """The db path for resolving numeric fact ids, or None when there is no db (source_keys still work)."""
    return DB_PATH if os.path.exists(DB_PATH) else None


@app.command("review-pending", help="List pending (unreviewed) facts, oldest first.")
def cmd_review_pending(as_json: bool = JSON_OPT):
    try:
        rows = _query(review.list_pending)
    except sqlite3.OperationalError as e:
        _fail(f"review-pending failed: {e} (rebuild knowledge.db with the current schema)")
    if as_json:
        _emit_json(rows)
        return
    if not rows:
        print("No pending facts.")
        return
    for r in rows:
        via = f"  via={r['captured_via']}" if r["captured_via"] else ""
        print(f"#{r['id']:<5} [{r['subject']}] ({r['trust_level']})  {r['source_key']}  added {r['date_added']}{via}")
        print(f"       {r['statement']}")
        if r["source_quote"]:
            print(f"       quote: {r['source_quote']}")
    print(f"\n{len(rows)} pending. Approve with `approve REF...` (or `--all`), reject with `reject REF... --reason TEXT`.")


@app.command("approve", help="Approve pending facts (REF is a fact id or a source_key): appends an 'active' revision "
                             "and commits it to the data repo. Only pending facts change; others are skipped.")
def cmd_approve(
    refs: Optional[List[str]] = typer.Argument(None, help="Fact ids or source_keys."),
    reason: str = typer.Option("approved", "--reason", help="Why; recorded as the revision's change_reason."),
    all_: bool = typer.Option(False, "--all", help="Approve every pending fact."),
    allow_dirty: bool = ALLOW_DIRTY_OPT_REVIEW,
    as_json: bool = JSON_OPT,
):
    refs = list(refs or [])
    if all_ and refs:
        raise typer.BadParameter("give REF... or --all, not both")
    if not all_ and not refs:
        raise typer.BadParameter("give at least one REF, or --all")
    res = review.approve(["all"] if all_ else refs, reason=reason, via="cli", allow_dirty=allow_dirty, db=_review_db())
    _report_review(res, as_json)


@app.command("reject", help="Reject pending facts (REF is a fact id or a source_key): appends a 'retracted' revision. "
                            "Only pending facts change; others are skipped.")
def cmd_reject(
    refs: List[str] = typer.Argument(..., help="Fact ids or source_keys."),
    reason: str = typer.Option(..., "--reason", help="Why; recorded as the revision's change_reason."),
    allow_dirty: bool = ALLOW_DIRTY_OPT_REVIEW,
    as_json: bool = JSON_OPT,
):
    res = review.reject(list(refs), reason, via="cli", allow_dirty=allow_dirty, db=_review_db())
    _report_review(res, as_json)


# ---- privacy (#31): `privacy check|rules|tag|untag|add-keyword|remove-keyword` ----

privacy_app = typer.Typer(help="Privacy rules: check what visibility a statement would get, and edit the rules "
                               "(subject tags and keywords). The rules file is edited only through these commands.",
                          pretty_exceptions_enable=False, rich_markup_mode=None)
app.add_typer(privacy_app, name="privacy")


class Requested(str, enum.Enum):
    normal = "normal"
    private = "private"


def _load_rules():
    try:
        return privacy.load_rules(privacy.rules_path())
    except privacy.PrivacyRulesError as e:
        _fail(e)


def _db_context(rules):
    """`rules` with the subject tree and the known-subject list from the live db (read-only) plus
    subjects already in general_facts.json (same notion of "known" as add-fact). No db -> empty
    context, so the unknown-subject rule is not enforced (nothing to compare against)."""
    try:
        con = connect()
    except DatabaseNotFound:
        return rules
    try:
        rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
    except sqlite3.Error:
        return rules
    finally:
        con.close()
    parents = {n: p for n, p in rows}
    known = set(parents)
    try:
        known |= {e["subject"] for e in add_fact._read_array(add_fact.DATA_PATH)
                  if isinstance(e, dict) and isinstance(e.get("subject"), str)}
    except add_fact.DataFileError:
        pass  # a corrupt facts file is add-fact's problem to report, not a reason to fail a check
    return rules.with_context(parents=parents, known_subjects=known)


def _resolution_json(res):
    return {"visibility": res.visibility, "raised_above_request": res.raised_above_request,
            "explanation": res.explain(),
            "reasons": [{"kind": r.kind, "detail": r.detail, "forces_private": r.forces_private} for r in res.reasons]}


@privacy_app.command("check", help="Resolve the visibility a statement would be stored with, and name the rule that raised it.")
def cmd_privacy_check(statement: str,
                      subject: str = typer.Option(..., "--subject", help="Subject the fact would be filed under."),
                      requested: Requested = typer.Option(Requested.normal, "--requested",
                                                          help="What the caller asks for; default normal shows what the rules alone do."),
                      as_json: bool = JSON_OPT):
    res = privacy.check(subject, statement, _db_context(_load_rules()), requested=requested.value)
    _emit_json(_resolution_json(res)) if as_json else print(res.explain())


@privacy_app.command("rules", help="Show the loaded privacy rules (subject tags and keywords).")
def cmd_privacy_rules(as_json: bool = JSON_OPT):
    path = privacy.rules_path()
    rules = _load_rules()
    exists = os.path.exists(path)
    if as_json:
        _emit_json({"path": path, "exists": exists, "version": privacy.VERSION,
                    "subject_tags": dict(sorted(rules.subject_tags.items())), "keywords": sorted(rules.keywords)})
        return
    print(f"Privacy rules: {path}" + ("" if exists else "  (no rules file; empty rules)"))
    if rules.subject_tags:
        print("subject tags:")
        for name, tag in sorted(rules.subject_tags.items()):
            print(f"  {name}: {tag}")
    else:
        print("subject tags: (none)")
    if rules.keywords:
        print("keywords:")
        for kw in sorted(rules.keywords):
            print(f"  - {kw}")
    else:
        print("keywords: (none)")


def _edit_rules(edit, describe, allow_dirty, dry_run):
    """Shared body of the rules-editing commands, mirroring add-fact's git safety net (#10):
    load, apply the library edit, refuse on a dirty private repo, write, commit only the rules file.
    `edit(rules) -> (new_rules, changed)`; `describe(old, new) -> (what, commit_message)`.
    An edit that changes nothing writes and commits nothing (and needs no clean tree)."""
    path = privacy.rules_path()
    rules = _load_rules()
    try:
        new, changed = edit(rules)
    except privacy.PrivacyRulesError as e:
        _fail(e)
    if not changed:
        print(f"no change: {path} already has this rule state")
        return
    what, message = describe(rules, new)
    directory = os.path.dirname(os.path.abspath(path))
    probe = directory
    while not os.path.isdir(probe):  # the data dir may not exist before the first rule
        probe = os.path.dirname(probe)
    try:
        repo = private_git.find_repo(probe)
        if repo is None:
            print(f"note: {directory} is not inside a git repository; the change will not be committed.", file=sys.stderr)
        elif not allow_dirty:
            private_git.ensure_clean_tree(repo)
    except private_git.PrivateGitError as e:
        _fail(e)
    if dry_run:
        print(f"dry run: would {what} in {path}" + (f" and commit {message!r}" if repo else "") + "; nothing written")
        return
    os.makedirs(directory, exist_ok=True)
    privacy.save_rules(new, path)
    print(f"Updated {path}: {what}.")
    if repo is None:
        return
    try:
        commit = private_git.commit_private_change([path], message, repo)
        detached = private_git.is_detached(repo)
    except private_git.PrivateGitError as e:
        print(f"error: the rule IS written to {path} but is NOT committed: {e}", file=sys.stderr)
        raise typer.Exit(3)
    print(f"Committed {commit} in {repo}: {message}")
    if detached:
        print(f"warning: {repo} has a detached HEAD; that commit is not on any branch.", file=sys.stderr)


ALLOW_DIRTY_OPT = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private (deliberate batch edits only); the commit still contains only the rules file.")
DRY_RUN_OPT = typer.Option(False, "--dry-run", help="Report what would change and commit; write nothing.")


class Tag(str, enum.Enum):
    private = "private"
    normal = "normal"


@privacy_app.command("tag", help="Tag a subject private (inherited by its children); --tag normal only registers it as a known subject.")
def cmd_privacy_tag(subject: str, tag: Tag = typer.Option(Tag.private, "--tag"),
                    allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    _edit_rules(lambda r: privacy.tag_subject(r, subject, tag.value),
                lambda old, new: (f"tag subject {subject} {tag.value}", f"privacy: tag {subject} ({tag.value})"),
                allow_dirty, dry_run)


@privacy_app.command("untag", help="Remove a subject's tag.")
def cmd_privacy_untag(subject: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    _edit_rules(lambda r: privacy.untag_subject(r, subject),
                lambda old, new: (f"untag subject {subject}", f"privacy: untag {subject}"),
                allow_dirty, dry_run)


@privacy_app.command("add-keyword", help="Add a name/keyword; statements containing it as a whole word become private. Literal text, case-insensitive.")
def cmd_privacy_add_keyword(keyword: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    def describe(old, new):
        kw = next(k for k in new.keywords if k not in old.keywords)
        return f"add keyword {kw!r}", f"privacy: add keyword {kw!r}"
    _edit_rules(lambda r: privacy.add_keyword(r, keyword), describe, allow_dirty, dry_run)


@privacy_app.command("remove-keyword", help="Remove a keyword. Facts already stored private stay private.")
def cmd_privacy_remove_keyword(keyword: str, allow_dirty: bool = ALLOW_DIRTY_OPT, dry_run: bool = DRY_RUN_OPT):
    def describe(old, new):
        kw = next(k for k in old.keywords if k not in new.keywords)
        return f"remove keyword {kw!r}", f"privacy: remove keyword {kw!r}"
    _edit_rules(lambda r: privacy.remove_keyword(r, keyword), describe, allow_dirty, dry_run)

import cli_lifecycle; app.add_typer(cli_lifecycle.app)  # supersede, retract, set-visibility (#8, #23)


import cli_migrate; cli_migrate.register(app)  # migrate-memory (#29)


from cli_inbox import register as _register_inbox; _register_inbox(app, DB_PATH)  # inbox, edit (#33)


def main(argv=None):
    """Run the CLI and return the exit code instead of exiting (used by tests)."""
    try:
        app(args=argv, prog_name="knowledge.py")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
