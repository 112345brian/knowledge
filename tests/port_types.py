"""Static checks for adapter modules against the ports declared in the `*_port.py` modules.

Run with ``uv run mypy``. These assignments supplement the runtime name/signature checks
in ``test_ports.py`` with mypy's structural type checks.
"""
from build_files_port import BuildFiles
from build_port import BuildOutputs, BuildSession, ProvenanceReader, SessionFactory, StepRegistry
from clock_port import Clock
from entity_files_port import EntityFiles
from entity_migration_port import EntityMigration
from entity_queries_port import EntityQueries
from facts_file_port import FactsFile
from ids_port import Ids
from lifecycle_reads_port import LifecycleReads
from memory_files_port import MemoryFiles
from private_git_port import PrivateGit
from privacy_rules_port import Rules
from revisions_port import Revisions
from review_reads_port import ReviewReads
from subject_files_port import SubjectFiles
import add_fact_store
import build
import build_info_store
import building
import local_buildfiles
import clock
import entities_store
import entity_migration_store
import entity_queries_store
import ids
import lifecycle_store
import migrate_memory_store
import privacy_store
import private_git
import review_store
import revisions_store
import subjects_store

git: PrivateGit = private_git
revisions: Revisions = revisions_store
rules: Rules = privacy_store
review_reads: ReviewReads = review_store
lifecycle_reads: LifecycleReads = lifecycle_store
facts_file: FactsFile = add_fact_store
clock_port: Clock = clock.SystemClock()
ids_port: Ids = ids
memory: MemoryFiles = migrate_memory_store
entity_files: EntityFiles = entities_store
entity_migration: EntityMigration = entity_migration_store
subject_files: SubjectFiles = subjects_store
entity_queries: EntityQueries = entity_queries_store
build_files: BuildFiles = local_buildfiles
provenance: ProvenanceReader = build_info_store.BuildProvenance(None, "", private_git)
build_session: BuildSession = building.Session(local_buildfiles, "", None, None)
build_sessions: SessionFactory = building.Sessions(local_buildfiles, "", None)
build_steps: StepRegistry = building.Registry(local_buildfiles, "")
build_outputs: BuildOutputs = build.NormalOutputs(local_buildfiles, None)
