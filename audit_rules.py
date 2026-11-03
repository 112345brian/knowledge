"""Which tables the live database audits and how its fact history is protected (docs/db-source-of-truth.md, "Logging"), pure.

Domain service (tach layer `domain_services`): the POLICY only, with no SQL. `live_schema_store` turns it into
triggers: for every audited table, one row in `audit_log` per insert, update and delete (the actor comes from
`audit_context`), and for the fact history an append-only guard.

`fact_revisions` is the fact history, so it is append-only, with one exception that already exists: the privacy
floor may raise a revision's `visibility` to 'private' (`privacy_store.raise_history_of_private_facts`). That column
and value are `RAISABLE_COLUMN` and `RAISED_VALUE`; no other change is allowed, and deleting a revision (also by
cascade from a deleted fact) is refused.
"""

__arbor__ = {"ring": "domain_services"}

import re
from typing import Iterable, List, Sequence, Tuple

# Curated tables whose every change is audited. `sources` is left out on purpose: its rows are mostly derived from the
# citation notes by an importer and would flood the log; revisit when the importers become upserts (increment 3).
AUDITED_TABLES = ("facts", "fact_sources", "subjects", "subject_aliases", "entities", "entity_aliases", "claims", "claim_facts")

REVISIONS_TABLE = "fact_revisions"
RAISABLE_COLUMN = "visibility"
RAISED_VALUE = "private"

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# (column name, 1-based position in the primary key or 0), as PRAGMA table_info reports them.
Column = Tuple[str, int]


def ident(name: str) -> str:
    """`name` if it is a plain SQL identifier, else ValueError (it is about to become part of a statement)."""
    if not _IDENT.fullmatch(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


def quote_ident(name: str) -> str:
    """`name` validated by `ident` and double-quoted, so a column or table named like an SQL keyword (order, group, ...)
    is still a valid identifier in a statement."""
    return f'"{ident(name)}"'


def key_columns(columns: Sequence[Column]) -> List[str]:
    """The primary-key column names of an audited table in key order; ValueError when it has no key."""
    keys = [ident(c) for c, pk in sorted(columns, key=lambda c: c[1]) if pk]
    if not keys:
        raise ValueError("an audited table needs a primary key")
    return keys


def trigger_names(tables: Iterable[str] = AUDITED_TABLES) -> List[str]:
    """Every trigger name `audit_trigger_sql` creates for `tables`, for tests and docs."""
    return [f"audit_{t}_{op}" for t in tables for op in ("insert", "update", "delete")]
