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
from build_repositories_port import FactRepository, UnitOfWork
from fact_ingest_port import FactRevisionWriter, PrivacyFloor
from facts_file_port import FactsFile
from ids_port import Ids
from lifecycle_reads_port import LifecycleReads
from memory_files_port import MemoryFiles
from music_export_port import MusicExportReads
from music_repository_port import MusicRepository
from normal_db_port import LeakScan, NormalSink, NormalSource
from private_git_port import PrivateGit
from privacy_rules_port import Rules
from revisions_port import Revisions
from sql_runner_port import SqlRunner
from review_reads_port import ReviewReads
from subject_files_port import SubjectFiles
from subject_repository_port import SubjectRepository
from entity_link_table_port import EntityLinkTable
from fact_privacy_table_port import FactPrivacyTable, PrivacyRulesReader
from fact_revision_table_port import FactRevisionTable, RevisionLogReader
import fact_tables_store
import local_stores
import sqlite3
import add_fact_store
import build
import build_info_store
import building
import local_buildfiles
import clock
import sqlite3
import entities_store
import entity_migration_store
import entity_queries_store
import fact_ingest_store
import ids
import leak_scan_store
import lifecycle_store
import local_stores
import migrate_memory_store
import music_export_store
import music_store
import normal_db_store
import privacy_store
import private_git
import review_store
import revisions_store
import sqlite3
import subject_repository_store
import sql_runner_store
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
music_export: MusicExportReads = music_export_store.open_reads("")
subjects_repository: SubjectRepository = subject_repository_store.SqliteSubjects(sqlite3.connect(":memory:"))
facts: FactRepository = fact_ingest_store.SqliteFactRepository(sqlite3.connect(":memory:"))
fact_revisions: FactRevisionWriter = fact_ingest_store.SqliteFactRevisions(sqlite3.connect(":memory:"))
unit_of_work: UnitOfWork = fact_ingest_store.SqliteUnitOfWork(sqlite3.connect(":memory:"))
privacy_floor: PrivacyFloor = local_stores
revision_log: RevisionLogReader = local_stores
rules_reader: PrivacyRulesReader = local_stores


def _sqlite_tables(con: sqlite3.Connection) -> None:
    revision_table: FactRevisionTable = fact_tables_store.SqliteFactRevisionTable(con)
    privacy_table: FactPrivacyTable = fact_tables_store.SqliteFactPrivacyTable(con)
    link_table: EntityLinkTable = fact_tables_store.SqliteEntityLinkTable(con)
from client.measurements_port import FactMeasurementLinks, MeasurementRepository  # noqa: E402
from client.vault_reader_port import VaultReader  # noqa: E402
from client import measurements_store, vault_reader_store  # noqa: E402
import sqlite3  # noqa: E402
_scratch = sqlite3.connect(":memory:")
vault_reader: VaultReader = vault_reader_store.SqliteVaultReader(_scratch)
measurement_repository: MeasurementRepository = measurements_store.SqliteMeasurementRepository(_scratch)
fact_measurement_links: FactMeasurementLinks = measurements_store.SqliteFactMeasurementLinks(_scratch)
music: MusicRepository = music_store.SqliteMusicRepository(sqlite3.connect(":memory:"))
normal_source: NormalSource = normal_db_store
normal_sink: NormalSink = normal_db_store
leak_scan: LeakScan = leak_scan_store
sql_runner: SqlRunner = sql_runner_store
build_files: BuildFiles = local_buildfiles
provenance: ProvenanceReader = build_info_store.BuildProvenance(None, "", private_git)
build_session: BuildSession = building.Session(local_buildfiles, "", None, None)
build_sessions: SessionFactory = building.Sessions(local_buildfiles, "", None)
build_steps: StepRegistry = building.Registry(local_buildfiles, "")
build_outputs: BuildOutputs = build.NormalOutputs(local_buildfiles, None)
