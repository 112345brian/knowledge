"""Static checks for adapter modules against the ports declared in the `*_port.py` modules.

Run with ``uv run mypy``. These assignments supplement the runtime name/signature checks
in ``test_ports.py`` with mypy's structural type checks.
"""
from build_files_port import BuildFiles
from clock_port import Clock
from datafiles_port import DataFiles
from entity_files_port import EntityFiles
from entity_migration_port import EntityMigration
from entity_queries_port import EntityQueries
from fact_reads_port import FactReads
from facts_file_port import FactsFile
from ids_port import Ids
from inputs_port import Inputs
from lifecycle_reads_port import LifecycleReads
from memory_files_port import MemoryFiles
from private_git_port import PrivateGit
from privacy_rules_port import Rules
from revisions_port import Revisions
from review_reads_port import ReviewReads
from script_runner_port import ScriptRunner
from step_stores_port import StepStores
from subject_files_port import SubjectFiles
import add_fact_store
import clock
import entities_store
import entity_migration_store
import entity_queries_store
import fact_reads_store
import ids
import lifecycle_store
import local_buildfiles
import local_datafiles
import local_inputs
import local_stores
import migrate_memory_store
import privacy_store
import private_git
import review_store
import revisions_store
import script_runner
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
fact_reads: FactReads = fact_reads_store
script_runner_port: ScriptRunner = script_runner
build_files: BuildFiles = local_buildfiles
datafiles: DataFiles = local_datafiles
inputs: Inputs = local_inputs
step_stores: StepStores = local_stores
