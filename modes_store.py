"""Adapter for the mode rules (#22): the SQL that applies `modes` to the knowledge db.

Everything a tool (the MCP server #2, the CLI `--mode` option) reads about facts must come through
this module. The filter lives in the SQL here (a join on facts.visibility), so a caller cannot forget
to apply it, and tests/test_modes.py scans the repo and fails when another module reads
facts / fact_revisions / facts_fts directly. What a mode may see is decided by the pure functions
in `modes`; this module only builds the queries and shapes the rows.

A fact that is not visible answers exactly like a fact that does not exist (None / [] / no row),
never "forbidden", so ids and source_keys cannot be probed. `con` needs `row_factory = sqlite3.Row`.
"""
import entities_store
import identifiers
import modes
import validtime
from modes import InvalidQuery
from timestamps import parse_as_of

__all__ = ["search_facts", "get_fact", "list_facts", "list_subjects", "get_history", "get_fact_as_of"]


def _gate(mode, include_pending, alias="f", sub="sub"):
    """(sql, params) restricting fact rows to what `mode` may see. Private mode: no restriction."""
    statuses = modes.visible_statuses(mode, include_pending)
    if statuses is None:
        return "", []
    marks = ",".join("?" for _ in statuses)
    return (f" AND {alias}.visibility = 'normal' AND {sub}.private = 0 AND {alias}.status IN ({marks})",
            statuses)


def _filters(sql, params, subject, trust, status, personal, kind=None, valid_at=None, entity=None, identifier=None):
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
            raise ValueError("valid_at must be a real date, YYYY-MM-DD")
        sql += " AND " + validtime.sql_valid_at()
        params.extend([valid_at, valid_at])
    if status:
        sql += " AND f.status = ?"
        params.append(status)
    if personal is True:
        sql += " AND f.is_personal = 1"
    elif personal is False:
        sql += " AND f.is_personal = 0"
    return sql, params


def search_facts(session, con, terms, subject=None, trust=None, personal=None, limit=20,
                 include_pending=False, kind=None, valid_at=None, entity=None, identifier=None):
    """Full-text search, best match first, visible facts only. Same row shape as
    knowledge.search_facts. An empty/blank query or invalid FTS syntax (unbalanced quote, ...)
    raises InvalidQuery with a generic message; the sqlite error text is not passed on."""
    mode = modes.readable(session, "searching")
    modes.check_search_terms(terms)
    limit = modes.check_limit(limit)
    gate, gparams = _gate(mode, include_pending)
    sql = """
        SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.kind, f.valid_from, f.valid_to, f.applies_to, f.statement
        FROM facts_fts
        JOIN facts f ON f.id = facts_fts.rowid
        JOIN subjects sub ON sub.id = f.subject_id
        WHERE facts_fts MATCH ?""" + gate
    params = [terms] + gparams
    sql, params = _filters(sql, params, subject, trust, None, personal, kind, valid_at, entity, identifier)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception as e:  # sqlite3.OperationalError for FTS syntax; checked by name, not type
        if type(e).__name__ == "OperationalError":
            raise InvalidQuery("The search text is not valid search syntax (check quotes and "
                               "operators), or has no searchable words.") from None
        raise


def list_facts(session, con, subject=None, trust=None, status=None, personal=None, limit=50,
               include_pending=False, kind=None, valid_at=None, entity=None, identifier=None):
    """Same row shape as knowledge.list_facts. A `status` filter can only narrow the visible set
    (normal mode asking for status 'superseded' gets [])."""
    mode = modes.readable(session, "listing facts")
    limit = modes.check_limit(limit)
    gate, gparams = _gate(mode, include_pending)
    sql = ("""SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.kind, f.valid_from, f.valid_to, f.applies_to, f.statement
              FROM facts f JOIN subjects sub ON sub.id = f.subject_id WHERE 1=1""" + gate)
    sql, params = _filters(sql, list(gparams), subject, trust, status, personal, kind, valid_at, entity, identifier)
    sql += " ORDER BY f.id LIMIT ?"
    params.append(limit)
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _ref_column(ref):
    if isinstance(ref, bool) or not isinstance(ref, (int, str)):
        raise TypeError("ref must be a fact id (int) or a source_key (str)")
    return "f.id" if isinstance(ref, int) else "f.source_key"


def get_fact(session, con, ref, include_pending=False):
    """One visible fact (shape of knowledge.get_fact, with `sources`), or None. A private,
    hidden, or nonexistent ref all give the same None. `ref` is an id or a source_key."""
    mode = modes.readable(session, "reading a fact")
    gate, gparams = _gate(mode, include_pending)
    f = con.execute(
        f"""SELECT f.*, sub.name AS subject, vf.path AS origin_path
            FROM facts f
            JOIN subjects sub ON sub.id = f.subject_id
            LEFT JOIN vault_files vf ON vf.id = f.origin_file_id
            WHERE {_ref_column(ref)} = ?""" + gate,
        [ref] + gparams).fetchone()
    if not f:
        return None
    out = modes.hide_fields(dict(f), mode)
    out["sources"] = [dict(s) for s in con.execute(
        """SELECT s.name, fs.locator
           FROM fact_sources fs JOIN sources s ON s.id = fs.source_id
           WHERE fs.fact_id = ?""", (f["id"],)).fetchall()]
    return out


def list_subjects(session, con):
    """Subjects with the count of VISIBLE facts. Normal mode: a subject appears only if it is not
    tagged private and has a visible fact, or is the parent of one that appears; a parent that
    is itself hidden is reported as parent=None so its name does not leak."""
    mode = modes.readable(session, "listing subjects")
    gate, gparams = _gate(mode, False)
    rows = con.execute(
        f"""SELECT s.id, s.name, s.domain, s.parent_id, s.private, p.name AS parent, COUNT(f.id) AS n_facts
            FROM subjects s
            LEFT JOIN subjects p ON p.id = s.parent_id
            LEFT JOIN facts f ON f.subject_id = s.id AND f.id IN (
                SELECT f.id FROM facts f JOIN subjects sub ON sub.id = f.subject_id WHERE 1=1 {gate})
            GROUP BY s.id
            ORDER BY s.domain, COALESCE(p.name, s.name), s.name""", gparams).fetchall()
    return modes.visible_subjects(rows, mode)


def _revisions(session, con, ref, include_pending, what):
    """Revisions of a VISIBLE fact; in normal mode only revision snapshots stored 'normal'."""
    mode = modes.readable(session, what)
    gate, gparams = _gate(mode, include_pending)
    cur = con.execute(
        f"""SELECT f.id AS fact_id, sub.name AS subject, r.*
            FROM fact_revisions r JOIN facts f ON f.id = r.fact_id JOIN subjects sub ON sub.id = f.subject_id
            WHERE {_ref_column(ref)} = ?""" + gate + " ORDER BY r.revision",
        [ref] + gparams)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    for r in rows:
        r.pop("id", None)
    return rows, mode


def get_history(session, con, ref, include_pending=False):
    """Every revision in order (shape of revisions_store.get_history). Normal mode: [] unless the fact
    is currently visible, and revisions stored private are left out. Unknown/hidden -> []."""
    rows, mode = _revisions(session, con, ref, include_pending, "reading history")
    return modes.visible_revisions(rows, mode)


def get_fact_as_of(session, con, ref, as_of, include_pending=False):
    """The revision in force at `as_of` (shape of revisions_store.get_fact_as_of), or None; see
    modes.revision_in_force for the normal-mode rule. Raises ValueError on a bad date."""
    parse_as_of(as_of)  # a bad date raises before any query
    rows, mode = _revisions(session, con, ref, include_pending, "reading history")
    return modes.revision_in_force(rows, mode, as_of)
