"""Adapter that gives a database the live-only tables and triggers (docs/db-source-of-truth.md, "Logging").

Infrastructure ring (tach layer `infrastructure`): it reads `live.sql` and the real columns of the audited tables,
then runs the SQL that `audit_rules` builds. Installing is idempotent, so it can run on every open of the live
database. A rebuild never calls it: the pipeline's schema (schema.sql) is unchanged.

The write use case sets who is writing with `set_context` before its transaction and clears it after; a write
with no context is audited as `via='unknown'`, never skipped.
"""

__arbor__ = {"ring": "infrastructure"}

import os
import sqlite3
from typing import List, Optional

import audit_rules

LIVE_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live.sql")


def _columns(con: sqlite3.Connection, table: str) -> Optional[List[audit_rules.Column]]:
    """(name, pk position) of `table`, or None when the table does not exist in this database."""
    rows = con.execute(f"PRAGMA table_info({audit_rules.ident(table)})").fetchall()
    return [(r[1], r[5]) for r in rows] or None


def install(con: sqlite3.Connection) -> List[str]:
    """Create the live tables and (re)create the audit and append-only triggers. Returns the names of the triggers
    installed. A curated table that this database lacks is skipped, not an error (a fixture may hold a subset)."""
    with open(LIVE_SQL, encoding="utf-8") as f:
        con.executescript(f.read())
    statements: List[str] = []
    for table in audit_rules.AUDITED_TABLES:
        columns = _columns(con, table)
        if columns:
            statements += audit_rules.audit_trigger_sql(table, columns)
    revisions = _columns(con, audit_rules.REVISIONS_TABLE)
    if revisions:
        statements += audit_rules.revisions_guard_sql(revisions)
    with con:
        for sql in statements:
            con.execute(sql)
    return [s.split()[2] for s in statements if s.startswith("CREATE TRIGGER")]


def set_context(con: sqlite3.Connection, via: str, session_id: Optional[str] = None, at: Optional[str] = None) -> None:
    """Say who is writing (and, optionally, the time the audit rows carry) for the writes that follow."""
    with con:
        con.execute("INSERT OR REPLACE INTO audit_context (id, via, session_id, at) VALUES (1, ?, ?, ?)", (via, session_id, at))


def clear_context(con: sqlite3.Connection) -> None:
    """Forget the actor; later writes are audited as 'unknown' until a context is set again."""
    with con:
        con.execute("DELETE FROM audit_context")
