"""Adapter that gives a database the live-only tables and triggers (docs/db-source-of-truth.md, "Logging").

Infrastructure ring (tach layer `infrastructure`): it reads `live.sql` and the real columns of the audited tables,
then builds and runs the trigger SQL from the policy in `audit_rules`. Installing is idempotent, so it can run on every open of the live
database. A rebuild never calls it: the pipeline's schema (schema.sql) is unchanged.

The write use case sets who is writing with `set_context` before its transaction and clears it after; a write
with no context is audited as `via='unknown'`, never skipped.
"""

__arbor__ = {"ring": "infrastructure"}

import os
import sqlite3
from typing import List, Optional, Sequence

import audit_rules

LIVE_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live.sql")


_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_ACTOR = ("COALESCE((SELECT at FROM audit_context WHERE id = 1), " + _NOW + "), "
          "COALESCE((SELECT via FROM audit_context WHERE id = 1), 'unknown'), "
          "(SELECT session_id FROM audit_context WHERE id = 1)")


def _row(prefix: str, columns: Sequence[audit_rules.Column]) -> str:
    return "json_object(" + ", ".join(f"'{audit_rules.ident(c)}', {prefix}.{c}" for c, _ in columns) + ")"


def _key(prefix: str, columns: Sequence[audit_rules.Column]) -> str:
    return "json_array(" + ", ".join(f"{prefix}.{c}" for c in audit_rules.key_columns(columns)) + ")"


def audit_trigger_sql(table: str, columns: Sequence[audit_rules.Column]) -> List[str]:
    """The DROP + CREATE statements of the three audit triggers (insert, update, delete) of `table`."""
    t = audit_rules.ident(table)
    out: List[str] = []
    for op, event, old, new, key in (("insert", "INSERT", None, "NEW", "NEW"), ("update", "UPDATE", "OLD", "NEW", "NEW"),
                                     ("delete", "DELETE", "OLD", None, "OLD")):
        name = f"audit_{t}_{op}"
        out.append(f"DROP TRIGGER IF EXISTS {name}")
        out.append(
            f"CREATE TRIGGER {name} AFTER {event} ON {t} BEGIN "
            f"INSERT INTO audit_log (changed_at, via, session_id, table_name, row_key, op, old_json, new_json) "
            f"SELECT {_ACTOR}, '{t}', {_key(key, columns)}, '{op}', "
            f"{_row(old, columns) if old else 'NULL'}, {_row(new, columns) if new else 'NULL'}; END")
    return out


def revisions_guard_sql(columns: Sequence[audit_rules.Column]) -> List[str]:
    """DROP + CREATE of the triggers that make the fact history append-only (see `audit_rules`)."""
    t = audit_rules.ident(audit_rules.REVISIONS_TABLE)
    raisable = audit_rules.ident(audit_rules.RAISABLE_COLUMN)
    others = " AND ".join(f"NEW.{audit_rules.ident(c)} IS OLD.{c}" for c, _ in columns if c != raisable)
    return [
        f"DROP TRIGGER IF EXISTS {t}_no_delete",
        f"DROP TRIGGER IF EXISTS {t}_update_guard",
        f"CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END",
        f"CREATE TRIGGER {t}_update_guard BEFORE UPDATE ON {t} "
        f"WHEN NOT (NEW.{raisable} = '{audit_rules.RAISED_VALUE}' AND {others}) "
        f"BEGIN SELECT RAISE(ABORT, '{t} is append-only (only {raisable} may be raised to {audit_rules.RAISED_VALUE})'); END",
    ]


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
            statements += audit_trigger_sql(table, columns)
    revisions = _columns(con, audit_rules.REVISIONS_TABLE)
    if revisions:
        statements += revisions_guard_sql(revisions)
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
