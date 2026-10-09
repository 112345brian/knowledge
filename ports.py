"""The ports: what the use cases need from the outside world (domain side: no I/O).

A use case (`review_service`, `lifecycle_service`, `add_fact_service`, `facts_batch_service`,
`migrate_memory_service`) takes a `Ports` bundle and calls only what is declared here. The adapters
(`revisions_store`, `review_store`, `private_git`, ...) implement these protocols; the facade modules
(`review`, `lifecycle`, `add_fact`, ...) are the composition roots that pick the adapters. A use case
therefore never imports an adapter, `sqlite3`, `subprocess` or `clock`, and a test can hand it fakes.

Adapters are plain modules: a module whose functions have these names and parameters satisfies the
protocol, which `tests/test_ports.py` checks, so a renamed adapter function fails a test and not a
caller at run time.
"""
import functools
import inspect
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Dict, List, Optional, Protocol, Tuple


class Git(Protocol):
    """The git safety net around knowledge-private (#10). Implemented by `private_git`."""
    PrivateGitError: type

    def find_repo(self, directory) -> Optional[str]: ...
    def ensure_clean_tree(self, repo_dir) -> None: ...
    def commit_private_change(self, paths, message, repo_dir) -> str: ...
    def is_detached(self, repo_dir) -> bool: ...


class Revisions(Protocol):
    """The entry files and the append-only revision log. Implemented by `revisions_store`."""

    def default_data_dir(self) -> str: ...
    def load_entries(self, data_dir=None) -> List[dict]: ...
    def read_log(self, path) -> List[Tuple[int, dict]]: ...
    def append_revision(self, source_key, changes, reason, via, session_id=None, data_dir=None,
                        revisions_path=None, expect=None) -> Any: ...


class Rules(Protocol):
    """The privacy rules file. Implemented by `privacy_store`."""

    def load_rules(self, path=None) -> Any: ...
    def rules_path(self, data_dir=None) -> str: ...


class ReviewReads(Protocol):
    """Reads behind the review queue. Implemented by `review_store`."""

    def default_data_dir(self) -> str: ...
    def db_exists(self, db_path) -> bool: ...
    def list_pending(self, db) -> List[dict]: ...
    def current_states(self, data_dir=None) -> Dict[str, dict]: ...
    def subject_parents(self, db) -> Dict[str, Optional[str]]: ...
    def source_key_for_id(self, db, fact_id) -> Tuple[bool, Optional[str]]: ...


class Reviews(Protocol):
    """What the lifecycle use case takes from the review use case. Implemented by the `review` facade."""

    def current_states(self, data_dir=None) -> Dict[str, dict]: ...
    def resolve_ref(self, ref, states, db) -> Tuple[Optional[str], Optional[str]]: ...
    def rules_with_db_context(self, data_dir, db) -> Any: ...
    def status_word(self, status) -> str: ...


class LifecycleReads(Protocol):
    """Reads behind the lifecycle decisions. Implemented by `lifecycle_store`."""
    DB_ERRORS: tuple

    def floor_inputs(self, data_dir, db) -> Any: ...
    def subjects(self, data_dir) -> Dict[str, Optional[str]]: ...


class FactsFile(Protocol):
    """The locked JSON array of new facts, plus the two db lookups. Implemented by `add_fact_store`."""

    def read_array(self, path) -> list: ...
    def append_record(self, path, record) -> int: ...
    def append_records(self, path, records, reject=None) -> Tuple[int, list]: ...
    def citekey_problem(self, db_path, citekey) -> Tuple[Optional[str], Optional[str]]: ...
    def subject_tree(self, db_path) -> Tuple[dict, Optional[set]]: ...
    def file_subjects(self, data_path) -> set: ...
    def data_dir_of(self, data_path) -> str: ...


class Clock(Protocol):
    """Implemented by `clock`."""

    def now(self) -> Any: ...
    def now_iso(self) -> str: ...


class Ids(Protocol):
    """Fresh identifiers (a source_key for a new fact)."""

    def new_source_key(self) -> str: ...


class FactDefaults(Protocol):
    """Where the facts file and the built db live (config, read on every call)."""
    data_path: str
    db_path: str


class MemoryFiles(Protocol):
    """Claude Code memory files on disk. Implemented by `migrate_memory_store`."""

    def root_exists(self, root) -> bool: ...
    def absolute_root(self, root) -> str: ...
    def discover(self, root) -> tuple: ...
    def read_memory_file(self, path, project, filename) -> Any: ...
    def existing_entries(self, data_dir) -> Dict[str, str]: ...
    def run_lock(self, data_path) -> ContextManager: ...
    def data_dir_of(self, data_path) -> str: ...


class AddFact(Protocol):
    """What batch / migrate take from the add-one-fact use case. Implemented by the `add_fact` facade."""

    def validate_fact(self, fact, db_path=None) -> Tuple[list, list]: ...
    def build_entry(self, fact, visibility=None) -> dict: ...
    def resolve_privacy(self, fact, data_path, db_path) -> Any: ...
    def append_fact(self, fact, data_path=None, db_path=None) -> Any: ...


@dataclass
class Ports:
    """A bundle of the ports a use case needs; unused ones stay None."""
    git: Optional[Git] = None
    revisions: Optional[Revisions] = None
    rules: Optional[Rules] = None
    reads: Optional[ReviewReads] = None
    reviews: Optional[Reviews] = None
    lifecycle_reads: Optional[LifecycleReads] = None
    facts_file: Optional[FactsFile] = None
    clock: Optional[Clock] = None
    ids: Optional[Ids] = None
    defaults: Optional[FactDefaults] = None
    memory: Optional[MemoryFiles] = None
    add_fact: Optional[AddFact] = None


def bind(fn: Callable, ports: Ports) -> Callable:
    """`fn(ports, *args, **kw)` as `bound(*args, **kw)`: the facade's public function. The signature and
    docstring are those of `fn` minus the leading `ports`, so introspection and help() still work."""
    @functools.wraps(fn)
    def bound(*args, **kwargs):
        return fn(ports, *args, **kwargs)

    params = list(inspect.signature(fn).parameters.values())[1:]
    bound.__signature__ = inspect.Signature(params, return_annotation=inspect.signature(fn).return_annotation)
    return bound


PORT_PROTOCOLS = {
    "git": Git, "revisions": Revisions, "rules": Rules, "reads": ReviewReads, "reviews": Reviews,
    "lifecycle_reads": LifecycleReads, "facts_file": FactsFile, "clock": Clock, "ids": Ids,
    "memory": MemoryFiles, "add_fact": AddFact,
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
