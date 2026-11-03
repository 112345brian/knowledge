"""Snapshot file names, retention and the restore-tables policy (docs/db-source-of-truth.md, "History and backup"), pure.

Domain service (tach layer `domain_services`): which names a snapshot gets, which snapshots a retention limit removes,
which tables `restore-tables` refuses to replace, and which foreign-key problems a restore introduced. Nothing here
touches a file or a database.

A snapshot is a complete, consistent copy of the database taken with SQLite's online backup API, named
`snapshot-<YYYYmmddTHHMMSS>.db` (`-2`, `-3` ... when taken in the same second). Only files with such names are ever
listed or pruned; anything else in the directory is left alone.
"""

__arbor__ = {"ring": "domain_services"}

import re
from typing import Iterable, List, Sequence, Tuple

DEFAULT_KEEP = 10
PREFIX = "snapshot-"
SUFFIX = ".db"
_NAME = re.compile(r"^snapshot-(?P<stamp>\d{8}T\d{6})(?:-(?P<n>\d+))?\.db$")

# The history tables: replacing their rows in place would rewrite history, so `restore-tables` refuses them. A whole
# database comes back from a snapshot or an export instead.
HISTORY_TABLES = ("fact_revisions", "audit_log", "operation_log")


class SnapshotError(Exception):
    """A snapshot or restore cannot proceed; the message says why."""


def snapshot_name(now) -> str:
    """The natural file name of a snapshot taken at the datetime `now`."""
    return f"{PREFIX}{now:%Y%m%dT%H%M%S}{SUFFIX}"


def candidate(name: str, attempt: int) -> str:
    """The `attempt`th (1-based) name to try for a snapshot whose natural name is `name`: `name`, `name-2`, ..."""
    if attempt == 1:
        return name
    return name[:-len(SUFFIX)] + f"-{attempt}" + SUFFIX


def _order(name: str) -> Tuple[str, int]:
    m = _NAME.fullmatch(name)
    return (m.group("stamp"), int(m.group("n") or 1)) if m else ("", 0)


def is_snapshot(name: str) -> bool:
    """Whether `name` is a snapshot file name."""
    return bool(_NAME.fullmatch(name))


def newest_first(names: Iterable[str]) -> List[str]:
    """The snapshot names among `names`, newest first."""
    return sorted((n for n in names if is_snapshot(n)), key=_order, reverse=True)


def next_attempt(name: str, existing: Iterable[str]) -> int:
    """The attempt number to start from for `name`: one past the highest existing `name`/`name-N`, so a pruned name is
    never reused (a reused name would sort as older than its neighbours and be pruned at once)."""
    stamp = _order(name)[0]
    highest = max((_order(n)[1] for n in existing if is_snapshot(n) and _order(n)[0] == stamp), default=0)
    return highest + 1


def to_prune(names: Iterable[str], keep: int = DEFAULT_KEEP) -> List[str]:
    """The snapshots to delete so only the `keep` newest remain. `keep` below 1 is an error: a retention of zero would
    delete the snapshot just taken."""
    if keep < 1:
        raise SnapshotError("keep must be at least 1")
    return newest_first(names)[keep:]


def check_restorable(tables: Sequence[str]) -> List[str]:
    """`tables` unchanged if each is a plain table name that may be restored in place; SnapshotError otherwise."""
    if not tables:
        raise SnapshotError("name at least one table to restore")
    for t in tables:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", t):
            raise SnapshotError(f"{t!r} is not a valid table name")
        if t in HISTORY_TABLES:
            raise SnapshotError(f"{t} is history and is never replaced in place; restore a whole database instead")
        if t in ("build_info", "build_inputs", "audit_context"):
            raise SnapshotError(f"{t} is not restorable")
    return list(dict.fromkeys(tables))


def new_violations(before: Iterable[str], after: Iterable[str]) -> List[str]:
    """The foreign-key problems in `after` that were not already in `before`: what a restore itself would break."""
    seen = set(before)
    return [v for v in after if v not in seen]
