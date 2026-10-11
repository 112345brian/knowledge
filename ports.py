"""Binding helper and conformance checks for the port protocols.

An application service (`review_service`, `lifecycle_service`, `add_fact_service`, `facts_batch_service`,
`migrate_memory_service`, `entities_service`, `subjects_service`, `rules_edit_service`) declares the ports it needs
as the fields of its own frozen `*Deps` dataclass, every one required, and its functions take that object first.
Each protocol lives in its own `*_port.py` module. The facade modules (`review`, `lifecycle`, `add_fact`,
`entities`, `subjects`, ...) are the composition roots: each builds its service's `*Deps` from the concrete
adapters and uses `bind` to publish the functions without the leading dependency argument. A service therefore
never imports an adapter, `sqlite3`, `subprocess` or `clock`, and a test can hand it fakes.

Adapters are plain modules: a module whose functions have these names and parameters satisfies the
protocol: `tests/test_ports.py` checks member names and parameter order, while `tests/port_types.py`
assigns adapters to protocols for static checking with mypy.
"""
import functools
import inspect
from typing import Any, Callable, List
from add_fact_port import AddFact
from clock_port import Clock
from entity_files_port import EntityFiles
from entity_migration_port import EntityMigration
from entity_queries_port import EntityQueries
from fact_defaults_port import FactDefaults
from facts_file_port import FactsFile
from ids_port import Ids
from lifecycle_reads_port import LifecycleReads
from memory_files_port import MemoryFiles
from private_git_port import PrivateGit
from privacy_rules_port import Rules
from revisions_port import Revisions
from review_reads_port import ReviewReads
from reviews_port import Reviews
from subject_files_port import SubjectFiles


def bind(fn: Callable, deps: Any) -> Callable:
    """`fn(deps, *args, **kw)` as `bound(*args, **kw)`: the facade's public function. The signature and
    docstring are those of `fn` minus the leading `deps`, so introspection and help() still work."""
    @functools.wraps(fn)
    def bound(*args, **kwargs):
        return fn(deps, *args, **kwargs)

    params = list(inspect.signature(fn).parameters.values())[1:]
    setattr(bound, "__signature__", inspect.Signature(params, return_annotation=inspect.signature(fn).return_annotation))
    return bound


PORT_PROTOCOLS = {
    "git": PrivateGit, "revisions": Revisions, "rules": Rules, "reads": ReviewReads, "reviews": Reviews,
    "lifecycle_reads": LifecycleReads, "facts_file": FactsFile, "clock": Clock, "ids": Ids,
    "memory": MemoryFiles, "add_fact": AddFact, "defaults": FactDefaults,
    "entity_files": EntityFiles, "entity_migration": EntityMigration,
    "subject_files": SubjectFiles, "entity_queries": EntityQueries,
}


def conformance_problems(name: str, adapter: Any) -> List[str]:
    """What is missing or mis-shaped in `adapter` against the protocol for port `name` (empty = conforms)."""
    proto = PORT_PROTOCOLS[name]
    problems = []
    for member, value in vars(proto).items():
        if member.startswith("_") or member in ("__annotations__",):
            continue
        if not callable(value):
            continue
        impl = getattr(adapter, member, None)
        if impl is None:
            problems.append(f"{name}: {member} is missing")
            continue
        want = [p for p in inspect.signature(value).parameters if p != "self"]
        have = list(inspect.signature(impl).parameters)
        if have[:len(want)] != want:
            problems.append(f"{name}.{member}: parameters {have} do not start with {want}")
    for member in getattr(proto, "__annotations__", {}):
        if not hasattr(adapter, member):
            problems.append(f"{name}: attribute {member} is missing")
    return problems
