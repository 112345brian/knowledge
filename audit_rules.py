"""Row-audit and append-only trigger SQL for the live database (docs/db-source-of-truth.md, "Logging"), pure.

Domain service (tach layer `domain_services`): it builds SQL text from table and column names and runs nothing;
`live_schema_store` reads the real columns and executes it. Which tables are audited is decided here, and the audit
triggers write into `audit_log` (defined in live.sql) with the actor taken from `audit_context`.

`fact_revisions` is the fact history, so it is append-only too, with one exception that already exists: the privacy
floor may raise a revision's `visibility` to 'private' (`privacy_store.raise_history_of_private_facts`). The update
guard allows exactly that change and nothing else; deleting a revision (also by cascade from a deleted fact) is refused.
"""

__arbor__ = {"ring": "domain_services"}

import re
from typing import Iterable, List, Sequence, Tuple

# Curated tables whose every change is audited. `sources` is left out on purpose: its rows are mostly derived from the
# citation notes by an importer and would flood the log; revisit when the importers become upserts (increment 3).
AUDITED_TABLES = ("facts", "fact_sources", "subjects", "subject_aliases", "entities", "entity_aliases", "claims", "claim_facts")

REVISIONS_TABLE = "fact_revisions"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_ACTOR = ("COALESCE((SELECT at FROM audit_context WHERE id = 1), " + _NOW + "), "
          "COALESCE((SELECT via FROM audit_context WHERE id = 1), 'unknown'), "
          "(SELECT session_id FROM audit_context WHERE id = 1)")

# (column name, 1-based position in the primary key or 0), as PRAGMA table_info reports them.
Column = Tuple[str, int]


def ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


def _row(prefix: str, columns: Sequence[Column]) -> str:
    return "json_object(" + ", ".join(f"'{ident(c)}', {prefix}.{c}" for c, _ in columns) + ")"


def _key(prefix: str, columns: Sequence[Column]) -> str:
    keys = [c for c, pk in sorted(columns, key=lambda c: c[1]) if pk]
    if not keys:
        raise ValueError("an audited table needs a primary key")
    return "json_array(" + ", ".join(f"{prefix}.{ident(c)}" for c in keys) + ")"


def audit_trigger_sql(table: str, columns: Sequence[Column]) -> List[str]:
    """The DROP + CREATE statements of the three audit triggers (insert, update, delete) of `table`."""
    t = ident(table)
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


def revisions_guard_sql(columns: Sequence[Column]) -> List[str]:
    """DROP + CREATE of the triggers that make `fact_revisions` append-only (see the module docstring)."""
    t = ident(REVISIONS_TABLE)
    others = " AND ".join(f"NEW.{ident(c)} IS OLD.{c}" for c, _ in columns if c != "visibility")
    return [
        f"DROP TRIGGER IF EXISTS {t}_no_delete",
        f"DROP TRIGGER IF EXISTS {t}_update_guard",
        f"CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END",
        f"CREATE TRIGGER {t}_update_guard BEFORE UPDATE ON {t} "
        f"WHEN NOT (NEW.visibility = 'private' AND {others}) "
        f"BEGIN SELECT RAISE(ABORT, '{t} is append-only (only visibility may be raised to private)'); END",
    ]


def trigger_names(tables: Iterable[str] = AUDITED_TABLES) -> List[str]:
    """Every trigger name `audit_trigger_sql` creates for `tables`, for tests and docs."""
    return [f"audit_{t}_{op}" for t in tables for op in ("insert", "update", "delete")]
