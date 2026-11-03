"""Take, list and restore database snapshots.

Application ring (tach layer `application`): `take_snapshot` makes a verified copy through the `Snapshots` port under a
name that never reuses an existing one, then prunes beyond the retention limit (a failure to prune is reported, not
fatal). `restore_snapshot` copies a verified snapshot to a NEW file and never overwrites. It never prints or exits.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass
from typing import List, Tuple

import snapshot_rules as rules
from clock_port import Clock
from snapshot_port import Snapshots


@dataclass(frozen=True)
class SnapshotDeps:
    """What the snapshot use cases reach the world through."""
    snapshots: Snapshots
    clock: Clock


@dataclass(frozen=True)
class SnapshotResult:
    """The snapshot taken, the ones pruned, and a warning if pruning failed."""
    path: str
    pruned: Tuple[str, ...]
    warning: str = ""


def take_snapshot(deps: SnapshotDeps, keep: int = rules.DEFAULT_KEEP) -> SnapshotResult:
    """Snapshot the database and keep only the `keep` newest snapshots."""
    rules.to_prune([], keep)                                  # a bad `keep` fails before anything is written
    natural = rules.snapshot_name(deps.clock.now())
    first = rules.next_attempt(natural, deps.snapshots.names())
    path = ""
    for attempt in range(first, first + 1000):
        try:
            path = deps.snapshots.create(rules.candidate(natural, attempt))
            break
        except FileExistsError:
            continue
    if not path:
        raise rules.SnapshotError(f"no free snapshot name for {natural}")
    pruned: List[str] = []
    warning = ""
    try:
        for old in rules.to_prune(deps.snapshots.names(), keep):
            deps.snapshots.remove(old)
            pruned.append(old)
    except OSError as e:
        warning = f"could not prune old snapshots: {e}"
    return SnapshotResult(path, tuple(pruned), warning)


def list_snapshots(deps: SnapshotDeps) -> List[str]:
    """The snapshot file names, newest first."""
    return rules.newest_first(deps.snapshots.names())


def restore_snapshot(deps: SnapshotDeps, source: str, into: str) -> int:
    """Copy the snapshot (any database file) at `source` to the new file `into`. Returns its schema version. Refuses a
    source that fails its integrity check, and never overwrites `into`."""
    version, problems = deps.snapshots.check(source)
    if problems:
        raise rules.SnapshotError(f"{source} is not sound: " + "; ".join(problems))
    deps.snapshots.copy_new(source, into)
    return version
