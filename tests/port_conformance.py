"""Protocol conformance checks for the port modules (`*_port.py`): is every adapter module shaped like the
protocol a composition root hands it to? Test support, not part of the application."""
import inspect
from typing import Any, List

from add_fact_port import AddFact
from clock_port import Clock
from entity_files_port import EntityFiles
from entity_migration_port import EntityMigration
from entity_queries_port import EntityQueries
from fact_reads_port import FactReads
from script_runner_port import ScriptRunner
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


PORT_PROTOCOLS = {
    "git": PrivateGit, "revisions": Revisions, "rules": Rules, "reads": ReviewReads, "reviews": Reviews,
    "lifecycle_reads": LifecycleReads, "facts_file": FactsFile, "clock": Clock, "ids": Ids,
    "memory": MemoryFiles, "add_fact": AddFact, "fact_reads": FactReads, "runner": ScriptRunner,
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
