"""Issue #22: the mode filter in the shared query layer, plus set_mode.

A `Session` holds one of three modes:

    off      every read and write raises ModeOff
    normal   (default on a fresh session) only facts that are stored visibility='normal',
             on a subject not tagged private, and ONLY status 'active' (plus 'pending' when the
             caller passes include_pending=True)
    private  everything

Everything a tool (the MCP server #2, the CLI `--mode` option) reads about facts must come
through this module. The filter lives in the SQL here (a join on facts.visibility), so a caller
cannot forget to apply it, and tests/test_modes.py scans the repo and fails when another module
reads facts / fact_revisions / facts_fts directly.

Rules the code enforces:
  * A fact that is not visible answers exactly like a fact that does not exist (None / [] / no
    row), never "forbidden", so ids and source_keys cannot be probed.
  * Counts only count visible facts. A subject with no visible fact is absent in normal mode.
  * Revision snapshots are filtered one by one: a revision stored private never comes back in
    normal mode, and a fact whose CURRENT visibility is private shows no history at all.
  * Claims, measurements and every other non-fact table are not exposed here.
  * Writes: `check_write` / `prepare_write` take the privacy.Resolution (#31), never a bare
    visibility string. The caller stores `resolution.visibility`; what the model asked for is only
    an input to the resolver. In private mode an unspecified request is 'private', never 'normal'.
  * Nothing here prints, exits, or writes to the db. Failures are exceptions (ModeError family).
"""
import enum
import threading

import privacy
import validtime
from revisions import parse_as_of, parse_timestamp

__all__ = [
    "Mode", "Session", "ModeError", "ModeOff", "InvalidMode", "WriteRefused", "InvalidQuery",
    "get_mode", "set_mode", "with_mode", "check_write", "prepare_write",
    "search_facts", "get_fact", "list_facts", "list_subjects", "get_history", "get_fact_as_of",
]

MAX_LIMIT = 1000

# Columns of a fact that normal mode never returns: the same ones normal_db.py leaves out of the
# normal-only database (a keyword heuristic, free text, a vault path, conversation provenance). A
# fact being "normal" says its statement may be shown, not that its notes or where it came from may.
NORMAL_HIDDEN_FIELDS = ("is_personal", "notes", "origin_file_id", "origin_path", "provided_by",
                        "captured_via", "session_id", "captured_at", "source_quote")


class Mode(str, enum.Enum):
    off = "off"
    normal = "normal"
    private = "private"


class ModeError(Exception):
    """Base class: the current mode does not allow this."""


class ModeOff(ModeError):
    def __init__(self, what="this operation"):
        super().__init__(f"The knowledge database is off, so {what} is not available. "
                         "Say \"database normal\" or \"database private\" to turn it on.")


class InvalidMode(ValueError):
    """set_mode got something that is not off | normal | private. The session is unchanged."""


class WriteRefused(ModeError):
    """A write that the current mode will not accept (private result in normal mode)."""


class InvalidQuery(ValueError):
    """The search text is empty or is not valid full-text syntax."""


class Session:
    """The current mode for ONE client conversation. No shared/global state: two Sessions never
    affect each other. A fresh session is `normal`."""

    __slots__ = ("_mode", "_lock")

    def __init__(self, mode=Mode.normal):
        self._lock = threading.Lock()
        self._mode = _coerce_mode(mode)

    @property
    def mode(self):
        return self._mode

    def _set(self, mode):
        with self._lock:
            previous, self._mode = self._mode, mode
        return previous

    def __repr__(self):
        return f"Session(mode={self._mode.value!r})"


def _coerce_mode(mode):
    if isinstance(mode, Mode):
        return mode
    if isinstance(mode, str):
        try:
            return Mode(mode)  # exact lowercase value only: no strip, no case folding
        except ValueError:
            pass
    raise InvalidMode(f"mode {mode!r} must be one of: off, normal, private")


def _session(session):
    if not isinstance(session, Session):
        raise TypeError("session must be a modes.Session")
    return session


def get_mode(session):
    return _session(session).mode.value


def set_mode(session, mode):
    """Validate, then switch. On an invalid value the session keeps its mode (and a later valid
    call works). Returns {"mode": new, "previous": old}.

    This is a USER action ("database private"). A server must not expose it as a tool the model
    can call on its own initiative."""
    new = _coerce_mode(mode)
    previous = _session(session)._set(new)
    return {"mode": new.value, "previous": previous.value}


def with_mode(session, data):
    """Wrap a tool result so every response states the mode it was produced under."""
    return {"mode": get_mode(session), "data": data}


# ------------------------------------------------------------------------------------ writes

def check_write(session, resolution):
    """Gate a write on the privacy resolution (#31). Returns the visibility to STORE (a str).
    off -> ModeOff; normal + resolved private -> WriteRefused; private mode accepts either.
    `resolution` must be a privacy.Resolution: a bare 'normal' string (e.g. what the model asked
    for) is a TypeError, so it cannot stand in for the resolved value."""
    mode = _session(session).mode
    if mode is Mode.off:
        raise ModeOff("writing")
    if not isinstance(resolution, privacy.Resolution):
        raise TypeError("resolution must be a privacy.Resolution from privacy.resolve_visibility")
    if resolution.visibility not in privacy.VALID_VISIBILITY:
        raise ValueError(f"resolution has invalid visibility {resolution.visibility!r}")
    if mode is Mode.normal and resolution.visibility == "private":
        raise WriteRefused(
            "This fact resolves to private (" + resolution.explain() + "), and the database is in "
            "normal mode. Say \"database private\" first, then save it again.")
    return resolution.visibility


def prepare_write(session, subject, statement, rules, requested=None):
    """Resolve visibility for a new fact through privacy.resolve_visibility, then gate it.
    Returns the privacy.Resolution; store `.visibility`. `requested=None` (the model said
    nothing) becomes 'private' in private mode and 'normal' in normal mode as the resolver's
    INPUT only; the rules can still raise it. Never defaults to 'normal' in private mode."""
    mode = _session(session).mode
    if mode is Mode.off:
        raise ModeOff("writing")
    if requested is None:
        requested = "private" if mode is Mode.private else "normal"
    resolution = privacy.resolve_visibility(subject, statement, requested, rules)
    stored = check_write(session, resolution)
    assert stored == resolution.visibility
    return resolution


# ------------------------------------------------------------------------------------ reads

def _readable(session, what):
    mode = _session(session).mode
    if mode is Mode.off:
        raise ModeOff(what)
    return mode


def _gate(mode, include_pending, alias="f", sub="sub"):
    """(sql, params) restricting fact rows to what `mode` may see. Private mode: no restriction."""
    if mode is Mode.private:
        return "", []
    statuses = ["active", "pending"] if include_pending else ["active"]
    marks = ",".join("?" for _ in statuses)
    return (f" AND {alias}.visibility = 'normal' AND {sub}.private = 0 AND {alias}.status IN ({marks})",
            statuses)


def _limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return limit


def _filters(sql, params, subject, trust, status, personal, kind=None, valid_at=None):
    if subject:
        sql += " AND sub.name = ?"
        params.append(subject)
    if trust:
        sql += " AND f.trust_level = ?"
        params.append(trust)
    if kind:
        sql += " AND f.kind = ?"
        params.append(kind)
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
                 include_pending=False, kind=None, valid_at=None):
    """Full-text search, best match first, visible facts only. Same row shape as
    knowledge.search_facts. An empty/blank query or invalid FTS syntax (unbalanced quote, ...)
    raises InvalidQuery with a generic message; the sqlite error text is not passed on."""
    mode = _readable(session, "searching")
    if not isinstance(terms, str) or "\x00" in terms or not any(c.isalnum() for c in terms):
        raise InvalidQuery("Search text is empty or invalid. Type some words to look for.")
    limit = _limit(limit)
    gate, gparams = _gate(mode, include_pending)
    sql = """
        SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.kind, f.valid_from, f.valid_to, f.statement
        FROM facts_fts
        JOIN facts f ON f.id = facts_fts.rowid
        JOIN subjects sub ON sub.id = f.subject_id
        WHERE facts_fts MATCH ?""" + gate
    params = [terms] + gparams
    sql, params = _filters(sql, params, subject, trust, None, personal, kind, valid_at)
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
               include_pending=False, kind=None, valid_at=None):
    """Same row shape as knowledge.list_facts. A `status` filter can only narrow the visible set
    (normal mode asking for status 'superseded' gets [])."""
    mode = _readable(session, "listing facts")
    limit = _limit(limit)
    gate, gparams = _gate(mode, include_pending)
    sql = ("""SELECT f.id, sub.name AS subject, f.trust_level, f.status, f.kind, f.valid_from, f.valid_to, f.statement
              FROM facts f JOIN subjects sub ON sub.id = f.subject_id WHERE 1=1""" + gate)
    sql, params = _filters(sql, list(gparams), subject, trust, status, personal, kind, valid_at)
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
    mode = _readable(session, "reading a fact")
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
    out = dict(f)
    if mode is not Mode.private:
        for hidden in NORMAL_HIDDEN_FIELDS:
            out.pop(hidden, None)
    out["sources"] = [dict(s) for s in con.execute(
        """SELECT s.name, fs.locator
           FROM fact_sources fs JOIN sources s ON s.id = fs.source_id
           WHERE fs.fact_id = ?""", (f["id"],)).fetchall()]
    return out


def list_subjects(session, con):
    """Subjects with the count of VISIBLE facts. Normal mode: a subject appears only if it is not
    tagged private and has a visible fact, or is the parent of one that appears; a parent that
    is itself hidden is reported as parent=None so its name does not leak."""
    mode = _readable(session, "listing subjects")
    gate, gparams = _gate(mode, False)
    rows = con.execute(
        f"""SELECT s.id, s.name, s.domain, s.parent_id, s.private, p.name AS parent, COUNT(f.id) AS n_facts
            FROM subjects s
            LEFT JOIN subjects p ON p.id = s.parent_id
            LEFT JOIN facts f ON f.subject_id = s.id AND f.id IN (
                SELECT f.id FROM facts f JOIN subjects sub ON sub.id = f.subject_id WHERE 1=1 {gate})
            GROUP BY s.id
            ORDER BY s.domain, COALESCE(p.name, s.name), s.name""", gparams).fetchall()
    by_id = {r["id"]: r for r in rows}
    if mode is Mode.private:
        shown = set(by_id)
    else:
        shown = {r["id"] for r in rows if r["n_facts"] > 0 and not r["private"]}
        for sid in list(shown):  # keep the (non-private) parents of a shown subject
            seen, cur = {sid}, by_id[sid]["parent_id"]
            while cur is not None and cur in by_id and cur not in seen:
                seen.add(cur)
                if not by_id[cur]["private"]:
                    shown.add(cur)
                cur = by_id[cur]["parent_id"]
    out = []
    for r in rows:
        if r["id"] not in shown:
            continue
        parent = r["parent"] if (r["parent_id"] in shown or mode is Mode.private) else None
        out.append({"name": r["name"], "domain": r["domain"], "parent": parent, "n_facts": r["n_facts"]})
    return out


def _revisions(session, con, ref, include_pending, what):
    """Revisions of a VISIBLE fact; in normal mode only revision snapshots stored 'normal'."""
    mode = _readable(session, what)
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
    """Every revision in order (shape of revisions.get_history). Normal mode: [] unless the fact
    is currently visible, and revisions stored private are left out. Unknown/hidden -> []."""
    rows, mode = _revisions(session, con, ref, include_pending, "reading history")
    if mode is not Mode.private:
        rows = [r for r in rows if r["visibility"] == "normal"]
    return rows


def get_fact_as_of(session, con, ref, as_of, include_pending=False):
    """The revision in force at `as_of` (shape of revisions.get_fact_as_of), or None. In normal
    mode None also when the revision in force then was private (the older normal revision is NOT
    substituted: it was not the state at that time). Raises ValueError on a bad date."""
    cutoff = parse_as_of(as_of)
    rows, mode = _revisions(session, con, ref, include_pending, "reading history")
    chosen = None
    for r in rows:
        if parse_timestamp(r["changed_at"]) <= cutoff:
            chosen = r
    if chosen is not None and mode is not Mode.private and chosen["visibility"] != "normal":
        return None
    return chosen
