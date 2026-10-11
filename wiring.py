"""Composition helper: verify a service's dependencies, then publish its functions with them bound.

An application service function takes its `*Deps` object first. A composition root builds that object from
the real adapters and calls `bind(fn, deps)` to publish `fn` without the leading argument. This lives in the
composition ring because binding is wiring, not domain: the domain ring holds only the port protocols
(`*_port.py`).

`verify(deps)` is what makes a mis-wired port fail at startup instead of on the one code path that happens to
call the missing or renamed method: every field of a `*Deps` must be a known port, and the adapter given for it
must have each of the protocol's methods with the protocol's parameters (in order) and attributes. A forgotten
adapter is already a TypeError (the Deps fields are required); this catches the wrong adapter, or a renamed one.
"""
import dataclasses
import functools
import inspect
from types import SimpleNamespace
from typing import Any, Callable, Iterable, List, Mapping, Union

from add_fact_port import AddFact
from clock_port import Clock
from docs_files_port import DocsFiles
from entity_files_port import EntityFiles
from entity_migration_port import EntityMigration
from entity_queries_port import EntityQueries
from fact_reads_port import FactReads
from facts_file_port import FactsFile
from ids_port import Ids
from lifecycle_reads_port import LifecycleReads
from locations import Locations
from memory_files_port import MemoryFiles
from private_git_port import PrivateGit
from privacy_rules_port import Rules
from review_reads_port import ReviewReads
from reviews_port import Reviews
from revisions_port import Revisions
from script_runner_port import ScriptRunner
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


def bind_all(service, deps: Any, names: Union[Iterable[str], Mapping[str, str]]) -> SimpleNamespace:
    """The public API of one composition root: `names` is either the service function names, or a mapping of
    public name -> service function name. Each is bound to `deps`; `deps` itself is kept as `.deps` so a test or
    a debugger can see what the root wired."""
    verify(deps)
    mapping = dict(names) if isinstance(names, Mapping) else {n: n for n in names}
    api = SimpleNamespace(**{public: bind(getattr(service, fn), deps) for public, fn in mapping.items()})
    api.deps = deps
    return api


PORT_PROTOCOLS = {
    "git": PrivateGit, "revisions": Revisions, "rules": Rules, "reads": ReviewReads, "reviews": Reviews,
    "lifecycle_reads": LifecycleReads, "facts_file": FactsFile, "clock": Clock, "ids": Ids,
    "memory": MemoryFiles, "add_fact": AddFact, "fact_reads": FactReads, "runner": ScriptRunner,
    "docs_files": DocsFiles, "entity_files": EntityFiles, "entity_migration": EntityMigration,
    "subject_files": SubjectFiles, "entity_queries": EntityQueries,
}


class PortMismatch(TypeError):
    """A dependency does not have the shape of the port it was wired to."""


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
        params = inspect.signature(impl).parameters
        if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params.values()):
            continue          # a (*args, **kwargs) forwarder (instrumentation, a patch): nothing to compare
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        have = [n for n, p in params.items() if p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)]
        if has_var_kw:    # `(fact, **kw)`: the named ones must lead the protocol's; the rest arrive as keywords
            if have != want[:len(have)]:
                problems.append(f"{name}.{member}: parameters {have} do not lead {want}")
            continue
        if have[:len(want)] != want:
            problems.append(f"{name}.{member}: parameters {have} do not start with {want}")
    for member in getattr(proto, "__annotations__", {}):
        if not hasattr(adapter, member):
            problems.append(f"{name}: attribute {member} is missing")
    return problems


def verify(deps: Any) -> Any:
    """Raise PortMismatch unless every dependency in `deps` (a frozen *Deps dataclass) is a known port and has the
    shape of its protocol. `locations` is a value, not a port. Returns `deps`."""
    problems = []
    for field in dataclasses.fields(deps):
        value = getattr(deps, field.name)
        if field.name == "locations":
            if not isinstance(value, Locations):
                problems.append(f"locations: expected a Locations, got {type(value).__name__}")
            continue
        if field.name not in PORT_PROTOCOLS:
            problems.append(f"{field.name}: not a known port (add it to wiring.PORT_PROTOCOLS)")
            continue
        problems.extend(conformance_problems(field.name, value))
    if problems:
        raise PortMismatch(f"{type(deps).__name__} is wired wrong:\n  " + "\n  ".join(problems))
    return deps
