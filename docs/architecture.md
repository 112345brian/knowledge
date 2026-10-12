# Architecture map (onion)

Two sub-packages hold the ETL: `ingest/` is the core pipeline and `client/` the optional client sources (a checkout enables
them with `CLIENT_SOURCES` in local_paths.py; see the README). Module names are unique across both, so the table below
uses the bare name; `ingest/` never imports `client/`.

The repo is an onion in flat modules: dependencies point only inward. The **model** (vocabularies, time and identifier
primitives) is the centre and imports nothing else in the repo. **Domain services** (the pure rules and the port
protocols in individual `*_port.py` modules) wrap it. **Application services** (`*_service`) orchestrate the domain
through the ports. **Infrastructure** (`*_store`, `clock`, `paths`, `private_git`, ...) implements the ports. The
**composition roots** (facades) bind an application service to its adapters, and the CLI, the inbox and the ingest
scripts are the driving adapters on the outside. Each application service declares the ports it needs as the required fields of its own frozen `*Deps` class (no
shared optional bundle); `wiring.py` holds `bind_all` and `verify`: every `compose()` checks each adapter against its port at startup.
The knowledge is personal, so the domain core is only what every knowledge base needs (facts, trust, privacy, revisions,
sources). Concerts, music ratings or measurements are not domain concepts here: they are optional client sources
(`client/`) on the outside, and their pure rule modules are marked `pipeline rules`, like the rule modules only the build pipeline uses.
The boundaries are enforced mechanically (`tach.toml`, the import-linter contracts in `pyproject.toml`, and
`tests/test_architecture.py` / `tests/test_ports.py`), and `tests/test_pipeline_golden.py` pins what the ingest
pipeline produces. `tach.toml` carries the same rings as its `layers`: `serving`, `cli`, `pipeline` (the driving
adapters), then `composition`, `infrastructure`, `application`, `domain_services`, `model`, outermost first, and a
test checks that every module's layer there agrees with its role in the table below. This table is checked by
`tests/test_architecture.py`: every module must appear exactly once, the *model* and *domain service* rows must equal
the domain contract list, and the *application service* rows must equal the use-case contract list.

```
driving adapters (knowledge, cli_*, inbox, build + the `ingest` package)
        |  call
        v
composition roots (review, lifecycle, add_fact, facts_batch, fact_ingest, migrate_memory, rules_edit, entities, subjects)
        |  bind                       (pick the adapters)
        v
infrastructure (*_store, clock, paths, private_git, ...)  --implements-->  port contracts (*_port.py)
                                                                                   ^
application services (*_service)  --------------------uses-------------------------+
        |  apply
        v
domain services (pure rules: privacy, modes, revisions, new_fact, ... *_rules, the *_port.py protocols)
        |  built from
        v
model (fact_rules, timestamps, validtime, textmatch, identifiers, source_status, entity_rules, fact_state, fact_entry, locations, pipeline_context, fixity_rules, acquisition_rules, source_record, subject_rules)   <- imports nothing else
```

## Rules

Each rule is a failing test or contract if broken.

Port adapters are checked at wiring time for member names and parameter order (`wiring.verify`, run by every `compose()`),
and statically for structural type compatibility (`tests/port_types.py`, `uv run mypy`).
The Git operations are declared by `PrivateGit`; composition roots inject `private_git`, and shared
private-file editing receives that port rather than importing the Git adapter.

The rules:

1. Domain modules (model and domain services) import no `sqlite3`/`os`/`pathlib`/`subprocess`/`uuid`/..., no adapter, no `clock`/`paths`/`private_git`
   (contract `domain-has-no-infrastructure`; the list can grow, never shrink).
2. Application services (`*_service`) import ports and domain only, never an adapter, a facade or infrastructure
   (contract `use-cases-depend-on-ports`; `os` only for `os.path` string handling, checked by an AST test).
3. The CLI and the inbox import no database, process or file-system module and no write-side adapter
   (contract `driving-adapters-no-infrastructure`).
4. Only `private_git` runs git and only `script_runner` spawns a Python subprocess; nothing above the domain
   imports upward (layers: serving > cli > pipeline > library).
5. Every adapter satisfies the protocol it is wired to: `wiring.verify` raises `PortMismatch` at startup unless every
   call the protocol allows (the minimal one, the full one, and one by parameter name) binds to the adapter's
   signature, and a forgotten adapter is a `TypeError` (the `*Deps` fields are required).
6. The model imports no other module of the repo, and a domain service never imports an application service
   (contract `model-is-innermost` and tach's `model` / `domain_services` layers).

### Where I/O lives

Raw file, sqlite-file and process calls (`open`, `os` file operations, `sqlite3.connect`, `shutil`, `subprocess`,
`tempfile`) appear only in infrastructure modules; a test enforces it for every module, with no exemptions.
Everything else reaches the disk through a port:

| Who | Port | Adapter |
| --- | --- | --- |
| application services | the `*_port` protocols they declare | the stores, `clock`, `private_git`, `script_runner` |
| build steps (`run(con, ctx)`) | `ctx.inputs` (`inputs_port`), `ctx.stores` (`step_stores_port`) | `local_inputs`, `local_stores` |
| maintenance tools (backfills, concerts cleanup, music export) | `DataFiles` (`datafiles_port`), `Inputs`, `MusicExportReads` (`music_export_port`); each tool's `run(...)` takes them, its `main()` takes them from the `Container` | `local_datafiles`, `local_inputs`, `music_export_store` |
| `build_service`, `build.py`, `normal_db`, `leak_scan_store`, `leak_test` | `BuildFiles` (`build_files_port`), the rebuild ports (`build_port`) | `local_buildfiles`, `building`, `build_info_store` |
| the normal-db use case | `NormalSource`, `NormalSink`, `LeakScan` (`normal_db_port`) | `normal_db_store`, `leak_scan_store` |
| `leak_test` (fixture, injection) | `SqlRunner` (`sql_runner_port`) | `sql_runner_store` |
| the CLI | the `Container` it is handed, whose `inputs` reads a facts file named on the command line | `container` |

A step imports no infrastructure module (a test checks tach's graph), and it writes only through the connection it
is given. Configuration reaches everything as `Locations`; the entry points (`container`, `build`, `normal_db`, the
tools) are the only code that reads it, once, with `paths.load()`. The clock is a `Clock` handed down like
everything else; nothing in the application holds ambient state.

### What is typed

`fact_state` gives the fact lifecycle (status, visibility, who may replace whom) real types, `fact_entry` does the
same for a fact entry and a revision record (`FactEntry`, `Revision`; `revisions` keeps its function names as thin
delegations), and `source_status` types a source's status (`SourceStatus`, `SourceState`) and its relations
(`Relation`), `entity_rules` an entity (`Entity`) and `subject_rules` a subject (`Subject`) with the parsing,
validation, matching and pure edits their files and lists are subject to.

## Module map

The rebuild (`build_service`, composed by `building`, entered through `build.py`) runs every step through a step
registry. A step is run the legacy way (its script is loaded and `run(con, ctx)` called) until its subsystem
registers a handler in `building.register_steps`; the change that registers a handler also removes the step's legacy
path. A failure in any stage raises `BuildError(stage)`; the swap is one replace of a finished temp file; a failure
after it keeps the new `knowledge.db` and removes the old `knowledge-normal.db`.

<!-- generated:module-map:start -->
<!-- Generated by `knowledge.py docs build` (generator: module-map). Do not edit by hand: change the code it is rendered from, then rebuild. -->

| Module | Layer | Role | What it is |
| --- | --- | --- | --- |
| `inbox` | serving | driving adapter | Local review inbox (#33): a small web page for the pending queue (#6), standard library only. |
| `cli_add_fact` | cli | driving adapter | `knowledge.py add-fact`: the Typer command over add_fact.record_fact (CLI-first rule #34). |
| `cli_audits` | cli | driving adapter | `knowledge.py audit-claims \| build-info \| audit-source-status \| audit-sources`: the premise and provenance audits. |
| `cli_build` | cli | driving adapter | `knowledge.py build` and registered client tools: commands that run a script (through `ctx.obj.scripts`). |
| `cli_common` | cli | driving adapter | Helpers and option definitions shared by the knowledge.py command modules (cli_facts, cli_audits, cli_review, cli_privacy, cli_build)… |
| `cli_docs` | cli | driving adapter | Typer commands for the generated docs (CLI-first rule #34): `docs build` rewrites them, `docs check` fails when stale. |
| `cli_entities` | cli | driving adapter | Typer commands for entities (#42): `entity list\|show\|add\|alias\|tag\|untag\|migrate-keywords`. |
| `cli_facts` | cli | driving adapter | `knowledge.py search \| show \| history \| subjects \| facts`: the read commands over the built db. |
| `cli_facts_batch` | cli | driving adapter | `knowledge.py add-facts`: the Typer command over facts_batch.add_facts (#32, CLI-first rule #34). |
| `cli_inbox` | cli | driving adapter | Typer commands for the inbox (#33): `inbox` (start the local server) and `edit`. |
| `cli_lifecycle` | cli | driving adapter | Typer commands for the fact lifecycle (#8, #23): supersede, retract, set-visibility. |
| `cli_migrate` | cli | driving adapter | Typer command `migrate-memory` (issue #29): a thin printer over migrate_memory.migrate(). |
| `cli_parity` | cli | driving adapter | CLI-first parity registry (issue #34). |
| `cli_privacy` | cli | driving adapter | `knowledge.py privacy check \| rules \| tag \| untag \| add-keyword \| remove-keyword`: the privacy rules. |
| `cli_review` | cli | driving adapter | `knowledge.py review-pending \| approve \| reject`: the review queue. |
| `cli_sources` | cli | driving adapter | Typer commands about sources (#48): `source ids CITEKEY`. |
| `cli_subjects` | cli | driving adapter | Typer commands for the subject hierarchy (#43): `subject list\|show\|alias\|describe\|deprecate`. |
| `knowledge` | cli | driving adapter | Single entry point for this repo (Typer CLI; run `uv sync` once, then `uv run python knowledge.py --help`). |
| `apply_fact_revisions` | pipeline | ETL adapter (driving, batch) | Apply fact_revisions.jsonl (issue #30): validate the append-only revision log, insert revisions \>= 2 into `fact_revisions`, and write each… |
| `backfill_dates` | pipeline | ETL adapter (driving, batch) | One-time backfill of `date_added` onto the legacy fact entries (issue #35). |
| `backfill_extracted_hashes` | pipeline | ETL adapter (driving, batch) | One-time backfill of the fixity baseline `extracted_from_sha256` onto legacy fact entries (#38). |
| `backfill_rules` | pipeline | pipeline rules | Byte-preserving backfill of keys into fact files, the rules (domain: no file, no lock). |
| `backfill_source_keys` | pipeline | ETL adapter (driving, batch) | One-time backfill of `source_key` onto the legacy fact entries (issue #30). |
| `build` | pipeline | ETL adapter (driving, batch) | Rebuild the configured knowledge database from scratch: schema + every seed/ingest script in this directory, in order. |
| `facts` | pipeline | ETL adapter (driving, batch) | Load pilot_facts.json (hand-authored) plus facts_batch1-4.json (extracted by background agents reading the vault's notes) into `facts`… |
| `leak_test` | pipeline | ETL adapter (driving, batch) | Leak test for knowledge-normal.db (#21). |
| `link_entities` | pipeline | ETL adapter (driving, batch) | Load entities.json (#42) and link facts to the entities they mention. |
| `link_source_relations` | pipeline | ETL adapter (driving, batch) | Load source-to-source relations (#41): `replaces` and `is-version-of`. |
| `literature_sources` | pipeline | ETL adapter (driving, batch) | Parse every citation note's (sources/\*.md, see paths.py -\> NOTES_VAULT) frontmatter into a `sources` row. |
| `normal_db` | pipeline | ETL adapter (driving, batch) | Build knowledge-normal.db (the entry point: it binds `normal_db_service` to the SQLite adapters; the whitelist and row decisions are in… |
| `seed_general_facts` | pipeline | ETL adapter (driving, batch) | Load general_facts.json (in the private data directory, not this repo): ad hoc facts with no separate source database behind them (no… |
| `seed_sources` | pipeline | ETL adapter (driving, batch) | Hand-authored/manual sources: the original pilot sources plus small manual instruments and deliberately-unlocatable carry-forwards that… |
| `seed_subject_hierarchy` | pipeline | ETL adapter (driving, batch) | Load the subject hierarchy (#43): parents, relation types, descriptions, aliases, deprecations. |
| `shared` | pipeline | ETL adapter (driving, batch) | Small helpers shared across the ingest scripts: the fact hints file and the generic get-or-create against a dimension table. |
| `source_ingest_rules` | pipeline | pipeline rules | Source ingest rules (steps 01 and 02), domain: no file, no db. |
| `source_readers` | pipeline | ETL adapter (driving, batch) | Composed source readers: objects that yield normalized `SourceRecord`s from manual_sources.json and from the vault's citation notes, so… |
| `add_fact` | composition | composition root | Composition root for the single-fact use case: binds `add_fact_service` to the real adapters (the facts file, the privacy rules, git, the… |
| `build_composition` | composition | composition root | Composition root of the build: opens a session and binds the ordered pipeline to it. |
| `building` | composition | composition root | Composition root of the database rebuild: binds `build_service` to its adapters and owns the session factory and the step registry. |
| `client_bootstrap` | composition | composition root | Composition: the one place the base looks for the client's registration, and the only place that does. |
| `container` | composition | composition root | The composition root of the whole application: one object that wires every use case for one `Locations` and one `Clock`. |
| `docs` | composition | composition root | Composition root for checking and rebuilding the generated documentation regions. |
| `entities` | composition | composition root | Composition root for entity command use cases. |
| `fact_ingest` | composition | composition root | The fact ingest (the facade: binds the use case in `fact_ingest_service` to the adapters a build step is given). |
| `facts_batch` | composition | composition root | Batch add_facts (the facade: binds the use case in `facts_batch_service` to the real adapters and keeps the public API): the library… |
| `lifecycle` | composition | composition root | Fact lifecycle (#8, #23): supersede, retract and set-visibility, each one appended revision. |
| `migrate_memory` | composition | composition root | Import Claude Code memory files as private, pending facts (issue #29). |
| `queries` | composition | composition root | Composition root for the read side: binds `queries_service` to the real read adapter (`fact_reads_store`). |
| `review` | composition | composition root | Review of pending facts (#6): list the queue, approve or reject items. |
| `rules_edit` | composition | composition root | Editing and reading the privacy rules file (#31): the facade (composition root) that binds the use case in `rules_edit_service` to the… |
| `scripts` | composition | composition root | Composition root for the script-backed commands: binds `scripts_service` to `script_runner`. |
| `subjects` | composition | composition root | Composition root for subject command use cases. |
| `wiring` | composition | composition helper | Composition helper: verify a service's dependencies, then publish its functions with them bound. |
| `acquisition_store` | infrastructure | infrastructure | Custodial history for sources and vault files (#46): when and how a file came into the collection. |
| `add_fact_store` | infrastructure | infrastructure | Adapter for adding facts: the locked, atomically written JSON array file, plus the two db lookups (a source citekey, the subject tree). |
| `build_info_store` | infrastructure | infrastructure | Build info and an inputs manifest (#47). |
| `claims_store` | infrastructure | infrastructure | Adapter for claims: the stale-premise audit (#1) and the claims repository the build seeds through. |
| `clock` | infrastructure | infrastructure | The system's clocks: where `Clock` (clock_port) is implemented. |
| `docs_store` | infrastructure | infrastructure | Adapter for `DocsFiles`: reads and writes the repository's own docs and sources under one root directory. |
| `entities_store` | infrastructure | infrastructure | Entities as first-class rows (#42): the people, organizations, places, projects and substances facts are about. |
| `entity_migration_store` | infrastructure | infrastructure | Moving privacy keywords onto private entities (#42). |
| `entity_queries_store` | infrastructure | infrastructure | Read-only entity and subject queries for the CLI. |
| `entity_repository_store` | infrastructure | infrastructure | SQLite adapter for the build's `EntityRepository` (`build_repositories_port`): the entities file as values, the entity and alias rows, and… |
| `fact_ingest_store` | infrastructure | infrastructure | SQLite adapters of the fact ingest: the part of `build_repositories_port.FactRepository` the ingest uses (not source-specific links) and… |
| `fact_queries` | infrastructure | infrastructure | Adapter for reading the built knowledge.db: the queries behind `knowledge.py search\|facts\|show\|subjects` and the connection helpers. |
| `fact_reads_store` | infrastructure | infrastructure | Adapter for `fact_reads_port.FactReads`: each read opens its own read-only connection to the built knowledge.db, runs one `fact_queries` /… |
| `fixity_store` | infrastructure | infrastructure | File fixity and identity for source files (#38). |
| `ids` | infrastructure | infrastructure | Adapter for fresh identifiers: a random source_key for a new fact (randomness is infrastructure, so the domain takes the key as an… |
| `leak_scan_store` | infrastructure | infrastructure | SQLite adapter for `normal_db_port.LeakScan`: it reads every cell of a normal DB and the private rows of the full DB, read-only. |
| `lifecycle_store` | infrastructure | infrastructure | Adapter for the fact lifecycle (#8, #23, #33): what the decisions need from the outside world. |
| `local_buildfiles` | infrastructure | infrastructure | Adapter for `build_files_port.BuildFiles`: the real disk, sqlite files and module loader. |
| `local_datafiles` | infrastructure | infrastructure | Adapter for `datafiles_port.DataFiles`: the real files, the revisions store's array reader and atomic writer, and the inter-process lock… |
| `local_inputs` | infrastructure | infrastructure | Adapter for `inputs_port.Inputs`: the real file system, the vault's sqlite database, and the file-fingerprint and acquisition-attribute… |
| `local_stores` | infrastructure | infrastructure | Adapter for `step_stores_port.StepStores`: the privacy, entity, subject, revision, claims and source stores, by name. |
| `locks` | infrastructure | infrastructure | Adapter for the inter-process file lock: the sidecar lock file and the exclusive lock on it (fcntl on POSIX, msvcrt on Windows). |
| `migrate_memory_store` | infrastructure | infrastructure | Adapter for migrate-memory (#29): finds and reads the memory files, holds the run lock, and lists the source keys already in the entry… |
| `modes_store` | infrastructure | infrastructure | Adapter for the mode rules (#22): the SQL that applies `modes` to the knowledge db. |
| `normal_db_store` | infrastructure | infrastructure | SQLite adapter for `normal_db_port.NormalSource` and `NormalSink`: it reads the full DB's rows and writes the normal DB. |
| `paths` | infrastructure | infrastructure | Reads where this person's data lives from the private data checkout, once, at an entry point. |
| `privacy_store` | infrastructure | infrastructure | Adapter for the privacy rules (driven side of the privacy domain). |
| `private_edit_store` | infrastructure | infrastructure | The shared edit-and-commit flow for hand-curated JSON files in knowledge-private (#43, #42). |
| `private_git` | infrastructure | infrastructure | Commit every mutation of the knowledge-private JSON files, so git is a reliable backup and safety net for them (issue #10). |
| `review_store` | infrastructure | infrastructure | Adapter for review (#6): the reads behind the review queue. |
| `revisions_store` | infrastructure | infrastructure | Adapter for the fact revision log (#30): the entry files, the log file and its lock, the clock and the db. |
| `script_runner` | infrastructure | infrastructure | Adapter for the standalone scripts (build.py and the client tools): `knowledge.py build` and each client tool are thin dispatches to them. |
| `source_repository_store` | infrastructure | infrastructure | SQLite adapter for `build_repositories_port.SourceRepository`: the sources, publishers, authors, identifiers, relations and vault_files… |
| `source_status_store` | infrastructure | infrastructure | Adapter for the source-status audit (#41): the SQL behind `knowledge.py audit-source-status`. |
| `sql_runner_store` | infrastructure | infrastructure | Adapter for `sql_runner_port.SqlRunner`: executes scripts and statements against a sqlite file. |
| `subject_repository_store` | infrastructure | infrastructure | SQLite adapter for `build_repositories_port.SubjectRepository` over the build database's subjects tables. |
| `subjects_store` | infrastructure | infrastructure | The subject hierarchy as data (#43). |
| `add_fact_service` | application | application service | Append one ad hoc fact: the use case. |
| `build_service` | application | application service | Rebuild the knowledge database: the use case behind `build.py` and `knowledge build`. |
| `docs_service` | application | application service | Keep the generated regions of README.md and docs/\*.md in step with the code: `build` rewrites them, `check` reports drift. |
| `entities_service` | application | application service | Entity command use cases. |
| `entity_link_service` | application | application service | Link facts to the entities they mention in a built database (#42), and prove no private mention is public. |
| `fact_ingest_service` | application | application service | The fact ingest (the use case, through the port protocols; `fact_ingest` binds it to the adapters). |
| `facts_batch_service` | application | application service | Batch add_facts (the use case, through the port protocols (`*_port`); `facts_batch` is the facade that binds it to the real adapters): the… |
| `identity_service` | application | application service | The identity operations every build step shares: which source and which subject a name means. |
| `lifecycle_service` | application | application service | Fact lifecycle (#8, #23): supersede, retract and set-visibility, each one appended revision. |
| `migrate_memory_service` | application | application service | Import Claude Code memory files as private, pending facts (issue #29). |
| `normal_db_service` | application | application service | Building the normal-tier database and checking it for leaks: the use case. |
| `privacy_floor_service` | application | application service | Apply the CURRENT privacy rules to every fact and subject of a built database (the privacy floor, #31). |
| `queries_service` | application | application service | The read side as an application service: look facts, subjects, history, audits and build info up through the `FactReads` port. |
| `review_service` | application | application service | Review of pending facts (#6): list the queue, approve or reject items. |
| `revision_apply_service` | application | application service | Apply the revision log to a built database, then re-assert the privacy floor (#30, #31). |
| `rules_edit_service` | application | application service | Edit the privacy rules file (#31): the use case behind `knowledge.py privacy tag\|untag\|add-keyword\| remove-keyword`, mirroring add-fact's… |
| `scripts_service` | application | application service | The commands that are a script run: rebuild the database, or a tool a client source ships. |
| `source_ingest_service` | application | application service | Application service for loading sources: store the records a reader produced, validate and store their relations, and record the vault… |
| `subject_hierarchy_service` | application | application service | Subject hierarchy use cases: load the hierarchy file's entries into the stored subjects, and find (or create) the subject a fact is filed… |
| `subjects_service` | application | application service | Subject command use cases. |
| `add_fact_port` | domain_services | domain service | Port exposing the single-fact use case to batch and migration flows. |
| `build_files_port` | domain_services | domain service | Port for the file-system work of building, swapping and checking database files: reading the schema, loading step modules, making the… |
| `build_port` | domain_services | domain service | Ports of the database rebuild that are not the file system (`build_files_port` is that one): the session a build writes through, the… |
| `build_records` | domain_services | domain service | The plain values a build step and the build repositories exchange: the core's (sources, subjects, facts, revisions, privacy and entities. |
| `build_repositories_port` | domain_services | domain service | Ports a build step uses to read and write the database being built: one repository per aggregate. |
| `build_rules` | domain_services | domain service | Build pipeline rules, domain: no file, no db, no clock. |
| `build_steps_port` | domain_services | domain service | The shape of a build step once it is bound to its dependencies: `run() -> StepResult`. |
| `claims_audit` | domain_services | domain service | Stale-premise audit for claims (#1). |
| `claims_rules` | domain_services | domain service | Claim normalization and the premise-matching policy for seeding claims (#44). |
| `client_registry` | domain_services | domain service | What the build knows about optional client sources, as data the client hands it: domain, no file, no db. |
| `clock_port` | domain_services | domain service | Port for the application's clock. |
| `datafiles_port` | domain_services | domain service | Port for the data files a maintenance tool reads and rewrites: locked, atomic, byte-exact. |
| `docs_files_port` | domain_services | domain service | Port for the files the documentation generator reads and writes: the docs, the two config files, and every module's source. |
| `docs_rules` | domain_services | domain service | Generated documentation, the rules: which doc regions are rendered from code, and how (domain: no file, no import of the code it describes). |
| `entity_files_port` | domain_services | domain service | Port for the persisted entity file: the I/O only. |
| `entity_link_rules` | domain_services | domain service | Which facts mention which entities, and the invariant that a private mention is stored private (pure). |
| `entity_migration_port` | domain_services | domain service | Port for migrating privacy keywords into private entities. |
| `entity_migration_rules` | domain_services | domain service | Moving privacy keywords onto private entities (#42): what the migration changes, and the proof it never lowers privacy (pure). |
| `entity_queries_port` | domain_services | domain service | Read port for entity links and subject hierarchy query results. |
| `fact_ingest_decisions` | domain_services | domain service | The decisions of the fact ingest (pilot/batch files and general_facts.json), domain: no file, no db. |
| `fact_ingest_port` | domain_services | domain service | Ports of the fact ingest that are not the build's repositories: which subject and vault file a name or path means, the revision a loaded… |
| `fact_ingest_rules` | domain_services | domain service | Fact ingest rules (steps 04 and 11), domain: no file, no db. |
| `fact_reads_port` | domain_services | domain service | Port for reading the built knowledge.db: what `knowledge.py search\|facts\|show\|subjects\|history\|audit-*\|build-info` need, without a… |
| `facts_batch_rules` | domain_services | domain service | Batch add_facts, the rules (domain: no file, db, git, clock or mode state). |
| `facts_file_port` | domain_services | domain service | Port for the persisted fact-entry file and its validation lookups. |
| `identity_rules` | domain_services | domain service | Identity rules the build shares, pure: how names and keys are normalized before they are compared. |
| `ids_port` | domain_services | domain service | Port for creating fresh source identifiers. |
| `inputs_port` | domain_services | domain service | Port for what a build step reads from outside the database: files, the vault's own database and their fingerprints. |
| `leak_rules` | domain_services | domain service | Leak test rules (domain: no sqlite, no files). |
| `lifecycle_reads_port` | domain_services | domain service | Read port for inputs used by lifecycle decisions. |
| `lifecycle_rules` | domain_services | domain service | Fact lifecycle (#8, #23, #33), the rules: result types and the pure decisions. |
| `memory_files_port` | domain_services | domain service | Port for discovering and reading source memory files. |
| `migrate_memory_rules` | domain_services | domain service | Import Claude Code memory files as private, pending facts (issue #29), the rules (domain: no file, db, git, clock or lock). |
| `modes` | domain_services | domain service | Issue #22: the mode rules (domain), plus set_mode. |
| `new_fact` | domain_services | domain service | A new fact, the rules (domain: no file, db, clock or randomness). |
| `normal_db_port` | domain_services | domain service | Ports for building the normal-tier database: what is read from the full DB, what is written to the normal one, and what the leak check… |
| `normal_rules` | domain_services | domain service | The normal tier's whitelist, the rules (domain: no sqlite, no files). |
| `privacy` | domain_services | domain service | Deterministic privacy rules (issue #31). |
| `privacy_rules_port` | domain_services | domain service | Port for reading and editing the persisted privacy rules (the CLI services' side). |
| `private_git_port` | domain_services | domain service | Application boundary for Git operations on the private data repository. |
| `review_reads_port` | domain_services | domain service | Read port for the pending review queue and current fact states. |
| `review_rules` | domain_services | domain service | Review of pending facts (#6), the rules: result types and the pure decisions behind the queue. |
| `reviews_port` | domain_services | domain service | Port exposing review operations to lifecycle use cases. |
| `revision_apply_rules` | domain_services | domain service | What applying the revision log to a built database means (pure; domain service). |
| `revisions` | domain_services | domain service | Fact revisions (#30): an append-only log of changes to facts, queryable as history. |
| `revisions_port` | domain_services | domain service | Port for persisted entries and the append-only revision log's writer. |
| `script_runner_port` | domain_services | domain service | Port for starting this repo's standalone scripts and tools as separate processes. |
| `sources_port` | domain_services | domain service | Port for sources: where normalized source records come from. |
| `sql_runner_port` | domain_services | domain service | Port for running SQL against a database file by path: the leak self-test's fixture builder and its marker-injection step. |
| `step_stores_port` | domain_services | domain service | Port for the data a build step loads from the private data files and the table writers that belong with them. |
| `subject_files_port` | domain_services | domain service | Port for editing the persisted subject hierarchy: the I/O only. |
| `acquisition_rules` | model | model | What a source's acquisition record may hold (when, how, from where, a note): the vocabulary and the validation of the data-supplied… |
| `entity_rules` | model | model | Entities as values: the vocabulary, the validation of an entities file, matching and the pure edits. |
| `fact_entry` | model | model | The fact entry and the revision record as types with behaviour. |
| `fact_rules` | model | model | Pure vocabulary shared by the fact-writing modules (domain: no I/O). |
| `fact_state` | model | model | A fact's lifecycle as types with behaviour: `Status`, `Visibility` and `FactState`. |
| `fixity_rules` | model | model | What a file fingerprint may hold: the vocabulary of a fixity record, free of how a file is read or hashed. |
| `identifiers` | model | model | Persistent identifiers for sources (#48): DOI, ISBN, ISSN, PMID, arXiv and `other`, normalized so the same work cannot hide behind two… |
| `locations` | model | model | Where this person's data lives, as one immutable value. |
| `pipeline_context` | model | model | What a build step is given besides its database connection: where the data lives and how to read it. |
| `source_record` | model | model | A source as one value: what the build knows about a citable work before it has a database id. |
| `source_status` | model | model | Source status over time, editions and source-to-source relations (#41). |
| `subject_rules` | model | model | Subjects as values: the vocabulary, the validation of a subjects file, canonical-name resolution and the pure edits. |
| `textmatch` | model | model | Literal whole-word term matching, shared by the privacy keyword list (#31) and entity aliases (#42). |
| `timestamps` | model | model | Pure timestamp parsing for revision times (domain: no clock, no I/O). |
| `validtime` | model | model | Valid time on facts (#40): when a fact was TRUE, as opposed to when it was recorded (date_added, revisions: transaction time). |
| `artist_directory` | client | domain service | The artists of one import run, keyed the one way the build compares artist names. |
| `artist_identity_service` | client | application service | The artist directory plus the configured group memberships, over the record-shaped music contract. |
| `artist_rules` | client | pipeline rules | Pure helpers for canonicalizing names from optional music sources, and the reading of artist_rules.json. |
| `backfill_snapshot_date` | client | ETL adapter (driving, batch) | One-time backfill of `measurements_snapshot.json` for the `measurements` client source (issue #35). |
| `bootstrap` | client | composition root | The client's registration with the build: which sources exist here, where their steps run, what they need. |
| `claims_port` | client | domain service | Port and records of the claims source's record-shaped repository contract. |
| `clean_concerts_csv` | client | ETL adapter (driving, batch) | One-time (but re-runnable/idempotent) cleanup of the concerts export CSV (see paths.py -\> CONCERTS_CSV): - strips stray whitespace from… |
| `concerts` | client | ETL adapter (driving, batch) | Ingest the concerts export CSV (see paths.py -\> CONCERTS_CSV) into the normalized artists / venues / festivals / concert_attendances tables. |
| `export_music_taste` | client | ETL adapter (driving, batch) | Export the full music-taste dataset from knowledge.db as one JSON file: every rated album, per-artist rollups (albums rated + concerts… |
| `link_fact_measurements` | client | ETL adapter (driving, batch) | Link facts to the measurements they cite (the `measurements` client source). |
| `measurement_rules` | client | pipeline rules | Measurement ingest rules (step 03), domain: no vault db, no knowledge db. |
| `measurements` | client | ETL adapter (driving, batch) | Ingest structured, repeatable numeric readings, lab results, manual measurements, activity summaries, and other local source records into… |
| `measurements_port` | client | domain service | Port for what the measurements load writes into the knowledge database: the metrics, measurements, exercises, foods, muscles and the… |
| `measurements_records` | client | domain service | The plain values of the measurements source's record-shaped repository contract (`measurements_port.MeasurementRepository`). |
| `measurements_service` | client | application service | Measurements use cases: load a vault's numeric readings into the knowledge database, and link facts to the measurements they cite. |
| `measurements_store` | client | infrastructure | SQLite adapters for the measurement aggregate of `build_repositories_port`: `MeasurementRepository` (the measurement, metric, exercise… |
| `music_export_port` | client | domain service | Port for what the music-taste export reads: albums, concerts and per-artist rollups as typed records. |
| `music_export_store` | client | infrastructure | Adapter for `music_export_port.MusicExportReads`: the SQL behind the music-taste export, run over the knowledge database at `db_path`. |
| `music_ingest` | client | composition root | Composition root for the client music steps: binds `music_service` to the SQLite music adapter for the build connection a step is given. |
| `music_ingest_rules` | client | pipeline rules | Music ingest rules (steps 07, 08, 09 and clean_concerts_csv), domain: no file, no db. |
| `music_port` | client | domain service | Port for the music tables the client music steps write: artists, venues, festivals, attendances, albums, tracks, scrobbles and the import… |
| `music_ratings` | client | ETL adapter (driving, batch) | Ingest the RYM ratings export (see paths.py -\> RYM_EXPORT_CSV) (a RateYourMusic ratings export) into the `albums` table, reusing the… |
| `music_records` | client | domain service | The plain values of the music source's record-shaped repository contract (`music_port.MusicRepository`). |
| `music_service` | client | application service | The music client steps as application services: the concerts CSV, the RYM ratings export, the Last.fm scrobble dump and the curated artist… |
| `music_store` | client | infrastructure | SQLite adapter for `client.music_port.MusicRepository`: the music tables of the build database it is given. |
| `scrobbles` | client | ETL adapter (driving, batch) | Ingest a Last.fm scrobble export into `tracks`/`scrobbles`, reusing `artists` (shared with concert_attendances/albums) for artist identity. |
| `seed_artist_members` | client | ETL adapter (driving, batch) | Seed artist_members from the curated `members` of artist_rules.json -- e.g. |
| `seed_claims` | client | ETL adapter (driving, batch) | Hand-authored broader claims, each citing the specific facts backing it. |
| `snapshot_date` | client | ETL adapter (driving, batch) | The private-data inputs of the vault-db measurements load: the adapter that reads `measurements_snapshot.json` (the snapshot date, issue… |
| `vault_reader_port` | client | domain service | Port for reading the vault's own database (the health/fitness vault's `bodybuilding.db`), as typed records. |
| `vault_reader_store` | client | infrastructure | SQLite adapter for `vault_reader_port.VaultReader`: the vault's own database, read table by table. |
<!-- generated:module-map:end -->

## Base / client boundary

The repository is a general-purpose machine that clients extend. Everything outside `client/` is the **base**
(the core, `ingest/` and the build); `client/` holds the **optional sources** a checkout enables with
`CLIENT_SOURCES` in its private `local_paths.py` (music: concerts, ratings, scrobbles; measurements; claims
seeding). Direction is one way:

- the base never imports `client.*` (shadow name `kn.<client module>`). Enforced by the import-linter contract
  "The base (core and ingest) never imports a client source", by `tests/test_client_boundary.py` (an AST scan of every
  non-client module) and by tach's deny-by-default `depends_on` (no base module lists a `client.*` path);
- a client module may import core types (ids, `UnitOfWork`, `Locations`, ports of the core), so a client source is
  onion-architected inside `client/` with client-owned ports (`client/music_port.py`, `client/music_records.py`,
  `client/music_service.py`, `client/music_store.py`, ...);
- the only seam from the base to a client source is data: `build_rules` lists the step files of each client source
  and the build loads them from the registry; a client source's own composition root (`client/music_ingest.py`)
  hands its ports to `wiring.bind_all(..., ports=...)`, so `wiring.PORT_PROTOCOLS` names no client port;
- the core's build contract (`build_repositories_port`, `build_records`) is the sources, subjects, facts,
  revisions, privacy and entities aggregates and the unit of work. `BuildSession` carries nothing of a client.

The claims *audit* (`claims_audit`, `claims_rules`, `claims_store`, the `audit-claims` CLI) stays in the base: the
`claims` and `claim_facts` tables are in the core `schema.sql` and the audit runs with `CLIENT_SOURCES=()`. Only the
seeding of claims from a private `claims.json` (`client/seed_claims.py`) is a client source.
