"""Adapter for reading the built knowledge.db: the queries behind `knowledge.py search|facts|show|subjects`
and the connection helpers. Read-only; takes a connection, returns plain dicts, never prints or exits.
(The mode-aware reads for the MCP server and the inbox live in `modes_store`.)
"""
import os
import pathlib
import sqlite3

import add_fact_store
import build_info_store
import entities_store
import fixity_store
import identifiers
import revisions_store
import source_status_store
import validtime
from new_fact import DataFileError
from paths import BODYBUILDING_VAULT, KNOWLEDGE_DB_DIR, PRIVATE_DATA_DIR

DB_PATH = os.path.join(os.path.expanduser(KNOWLEDGE_DB_DIR), "knowledge.db")
FACTS_FILE = os.path.join(PRIVATE_DATA_DIR, "general_facts.json")


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


def existing_db_path():
    """The db path for resolving numeric fact ids, or None when there is no db (source_keys still work)."""
    return DB_PATH if os.path.exists(DB_PATH) else None


def subject_context():
    """({subject: parent}, known subjects) from the live db plus the subjects already in general_facts.json
    (the same notion of "known" as add-fact), or None when there is no usable db (nothing to compare against)."""
    try:
        con = connect()
    except DatabaseNotFound:
        return None
    try:
        rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    parents = {n: p for n, p in rows}
    known = set(parents)
    try:
        known |= add_fact_store.file_subjects(FACTS_FILE)
    except DataFileError:
        pass  # a corrupt facts file is add-fact's problem to report, not a reason to fail a check
    return parents, known


def _visible_statuses(include_pending):
    """Statuses shown when the caller names none: active only (#6), plus pending on request.
    Superseded and retracted facts need an explicit status filter. Same rule as modes.py."""
    return ("active", "pending") if include_pending else ("active",)


def _filters(sql, params, subject=None, trust=None, status=None, personal=None, include_pending=False, kind=None, valid_at=None, entity=None, identifier=None):
    """Append the shared fact filters. `personal` is True / False / None (no filter).
    An explicit `status` wins and `include_pending` is then ignored; without one only
    active facts (and pending ones when `include_pending`) match."""
    if subject:  # a subject's alias (#43) selects the same facts as its name
        sql += " AND (sub.name = ? OR sub.id IN (SELECT subject_id FROM subject_aliases WHERE alias = ?))"
        params.extend([subject, subject])
    if trust:
        sql += " AND f.trust_level = ?"
        params.append(trust)
    if kind:
        sql += " AND f.kind = ?"
        params.append(kind)
    if identifier is not None:
        clause, extra = identifiers.filter_clause(identifier)
        sql += clause
        params.extend(extra)
    if entity is not None:
        clause, extra = entities_store.filter_clause(entity)
        sql += clause
        params.extend(extra)
    if valid_at is not None:
        if not validtime.is_valid_date(valid_at):
            raise ValueError(f"valid_at {valid_at!r} must be a real date, YYYY-MM-DD")
        sql += " AND " + validtime.sql_valid_at()
        params.extend([valid_at, valid_at])
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


def search_facts(con, terms, subject=None, trust=None, personal=None, limit=20, status=None, include_pending=False, kind=None, valid_at=None, entity=None, identifier=None):
    """Full-text search, best match first; active facts unless `status` / `include_pending` say otherwise. Raises sqlite3.OperationalError on FTS syntax errors."""
    sql = """
        SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.kind, f.valid_from, f.valid_to, f.applies_to, f.statement
        FROM facts_fts
        JOIN facts f ON f.id = facts_fts.rowid
        JOIN subjects sub ON sub.id = f.subject_id
        WHERE facts_fts MATCH ?
    """
    params = [terms]
    sql = _filters(sql, params, subject=subject, trust=trust, status=status, personal=personal,
                   include_pending=include_pending, kind=kind, valid_at=valid_at, entity=entity, identifier=identifier)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def list_facts(con, subject=None, trust=None, status=None, personal=None, limit=50, include_pending=False, kind=None, valid_at=None, entity=None, identifier=None):
    """Facts by id; active only unless `status` names one or `include_pending` adds pending."""
    sql = """SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.kind, f.valid_from, f.valid_to, f.applies_to, f.statement
             FROM facts f JOIN subjects sub ON sub.id = f.subject_id WHERE 1=1"""
    params = []
    sql = _filters(sql, params, subject=subject, trust=trust, status=status, personal=personal,
                   include_pending=include_pending, kind=kind, valid_at=valid_at, entity=entity, identifier=identifier)
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


def fact_as_of(con, fact_id, as_of):
    """('ok', revision) | ('no-fact', None) | ('no-history', None) | ('not-yet', None).
    Raises ValueError for a bad `as_of` (from revisions_store.get_fact_as_of)."""
    rev = revisions_store.get_fact_as_of(con, fact_id, as_of)
    if rev is not None:
        return "ok", rev
    if get_fact(con, fact_id) is None:
        return "no-fact", None
    return ("not-yet", None) if revisions_store.get_history(con, fact_id) else ("no-history", None)


QueryError = sqlite3.OperationalError  # raised for FTS syntax errors and for a db built with an older schema


def audit_sources(con):
    """(changed source files, facts with no baseline hash) since extraction (#38), searched under the vault."""
    return fixity_store.audit_sources(con, search_roots=[BODYBUILDING_VAULT]), fixity_store.unbaselined_facts(con)


def audit_source_status(con):
    """(facts citing a retracted / doubtful / superseded source, sources superseded with no replacement) (#41)."""
    return source_status_store.audit_source_status(con), source_status_store.superseded_without_replacement(con)


def latest_build_info(con):
    """The latest build row and its input manifest (#47), or None; see build_info_store.latest."""
    return build_info_store.latest(con)


compare_build_info = build_info_store.compare
