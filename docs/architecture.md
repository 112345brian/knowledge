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
adapters), then `composition`, `infrastructure`, `application`, `domain_services`, `model`, outermost first, and each
module's role below is derived from its layer there. The module map is generated from `tach.toml` and each
module's docstring (`knowledge.py docs build`), and `tests/test_docs.py` fails when it is stale; the role lists are
checked against the import-linter contracts by `tests/test_architecture.py`.

```
driving adapters (knowledge, cli_*, inbox, build + the `ingest` package)
        |  call
        v
composition roots (the `composition` layer in the module map below)
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
model (the `model` layer in the module map below)   <- imports nothing else
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
5. Every adapter satisfies the protocol it is wired to: `wiring.verify` raises `PortMismatch` at startup, and a forgotten
   adapter is a `TypeError` (the `*Deps` fields are required).
6. The model imports no other module of the repo, and a domain service never imports an application service
   (contract `model-is-innermost` and tach's `model` / `domain_services` layers).

### The pipeline is the outermost ring, not a client of ports

`build.py` and the `ingest/` and `client/` steps are batch drivers: their whole job is to populate the database.
They read their rules from pure modules (`*_ingest_rules`, `build_rules`, `revisions`, `privacy`, ...), write
through the stores, and import no facade, CLI or serving module (tach enforces it). They do not go through the
`*_port` protocols, because a port in front of "insert these rows" would only restate the SQL. What they share
with the rest of the onion is configuration: every step and `build.py` resolve their paths from
`paths.settings()` when they run, never at import, and a test can still set the module attribute
(`DATA_DIR`, `VAULT`, `CSV_PATH`, ...) to a path.

### What is still not typed

`fact_state` gives the fact lifecycle (status, visibility, who may replace whom) real types, and `fact_entry` does
the same for a fact entry and a revision record (`FactEntry`, `Revision`); `revisions` keeps its function names as
thin delegations. Sources (`source_status`) are still a vocabulary and functions over dicts.

## Module map

<!-- generated:module-map:start -->
<!-- Generated by `knowledge.py docs build` (generator: module-map). Do not edit by hand: change the code it is rendered from, then rebuild. -->

| Module | Layer | Role | What it is |
| --- | --- | --- | --- |
| `inbox` | serving | driving adapter | Local review inbox (#33): a small web page for the pending queue (#6), standard library only. |
| `cli_add_fact` | cli | driving adapter | `knowledge.py add-fact`: the Typer command over add_fact.record_fact (CLI-first rule #34). |
| `cli_docs` | cli | driving adapter | Typer commands for the generated docs (CLI-first rule #34): `docs build` rewrites them, `docs check` fails when stale. |
| `cli_entities` | cli | driving adapter | Typer commands for entities (#42): `entity list\|show\|add\|alias\|tag\|untag\|migrate-keywords`. |
| `cli_facts_batch` | cli | driving adapter | `knowledge.py add-facts`: the Typer command over facts_batch.add_facts (#32, CLI-first rule #34). |
| `cli_inbox` | cli | driving adapter | Typer commands for the inbox (#33): `inbox` (start the local server) and `edit`. |
| `cli_lifecycle` | cli | driving adapter | Typer commands for the fact lifecycle (#8, #23): supersede, retract, set-visibility. |
| `cli_migrate` | cli | driving adapter | Typer command `migrate-memory` (issue #29): a thin printer over migrate_memory.migrate(). |
| `cli_parity` | cli | driving adapter | CLI-first parity registry (issue #34). |
| `cli_sources` | cli | driving adapter | Typer commands about sources (#48): `source ids CITEKEY`. |
| `cli_subjects` | cli | driving adapter | Typer commands for the subject hierarchy (#43): `subject list\|show\|alias\|describe\|deprecate`. |
| `knowledge` | cli | driving adapter | Single entry point for this repo (Typer CLI; run `uv sync` once, then `uv run python knowledge.py --help`). |
| `apply_fact_revisions` | pipeline | ETL adapter (driving, batch) | Validates and applies the append-only revision log. |
| `artist_rules` | pipeline | pipeline rules | Pure helpers for canonicalizing names from optional music sources. |
| `backfill_dates` | pipeline | ETL adapter (driving, batch) | One-time backfill of `date_added` onto the legacy fact entries (issue #35). |
| `backfill_extracted_hashes` | pipeline | ETL adapter (driving, batch) | One-time backfill of the fixity baseline `extracted_from_sha256` onto legacy fact entries (#38). |
| `backfill_rules` | pipeline | pipeline rules | Byte-preserving backfill of keys into fact files, the rules (domain: no file, no lock). |
| `backfill_snapshot_date` | pipeline | ETL adapter (driving, batch) | One-time backfill of `measurements_snapshot.json` for the `measurements` client source (issue #35). |
| `backfill_source_keys` | pipeline | ETL adapter (driving, batch) | One-time backfill of `source_key` onto the legacy fact entries (issue #30). |
| `build` | pipeline | ETL adapter (driving, batch) | Runs the steps, swaps the db. |
| `build_rules` | pipeline | pipeline rules | Build pipeline rules, domain: no file, no db, no clock. |
| `clean_concerts_csv` | pipeline | ETL adapter (driving, batch) | Idempotent cleanup of the concerts CSV (concerts source). |
| `concerts` | pipeline | ETL adapter (driving, batch) | Concerts CSV export (concerts source). |
| `export_music_taste` | pipeline | ETL adapter (driving, batch) | Exports the music dataset as one JSON file (music sources). |
| `fact_ingest_rules` | pipeline | pipeline rules | Fact ingest rules (steps 04 and 11), domain: no file, no db. |
| `facts` | pipeline | ETL adapter (driving, batch) | Hand-authored pilot and batch fact files. |
| `leak_rules` | pipeline | pipeline rules | Leak test rules (domain: no sqlite, no files). |
| `leak_scan_store` | pipeline | ETL adapter (driving, batch) | Read-only database and file scanner for the normal-tier leak check. |
| `leak_test` | pipeline | ETL adapter (driving, batch) | Leak test for knowledge-normal.db (#21). Fails loudly when a private marker is found. |
| `link_entities` | pipeline | ETL adapter (driving, batch) | Load entities.json (#42) and link facts to the entities they mention. |
| `link_fact_measurements` | pipeline | ETL adapter (driving, batch) | Link facts to the measurements they cite (the `measurements` client source). |
| `link_source_relations` | pipeline | ETL adapter (driving, batch) | Load source-to-source relations (#41): `replaces` and `is-version-of`. |
| `literature_sources` | pipeline | ETL adapter (driving, batch) | Citation notes in the notes vault, parsed into sources. |
| `measurement_rules` | pipeline | pipeline rules | Measurement ingest rules (step 03), domain: no vault db, no knowledge db. |
| `measurements` | pipeline | ETL adapter (driving, batch) | Numeric readings and lab results (measurements source). |
| `music_ingest_rules` | pipeline | pipeline rules | Music ingest rules (steps 07, 08, 09 and clean_concerts_csv), domain: no file, no db. |
| `music_ratings` | pipeline | ETL adapter (driving, batch) | Ratings export (ratings source). |
| `music_shared` | pipeline | ETL adapter (driving, batch) | Helpers shared by optional music ingesters; source-specific identity rules live in private data. |
| `normal_db` | pipeline | ETL adapter (driving, batch) | SQL copy + file swap (also a script). |
| `normal_rules` | pipeline | pipeline rules | The normal tier's whitelist, the rules (domain: no sqlite, no files). |
| `scrobbles` | pipeline | ETL adapter (driving, batch) | Scrobble export (scrobbles source). |
| `seed_artist_members` | pipeline | ETL adapter (driving, batch) | Curated artist membership (music sources). |
| `seed_claims` | pipeline | ETL adapter (driving, batch) | Hand-authored broader claims, each citing the specific facts backing it. |
| `seed_general_facts` | pipeline | ETL adapter (driving, batch) | Ad hoc facts from `general_facts.json`. |
| `seed_sources` | pipeline | ETL adapter (driving, batch) | Manual sources from `manual_sources.json`. |
| `seed_subject_hierarchy` | pipeline | ETL adapter (driving, batch) | Load the subject hierarchy (#43): parents, relation types, descriptions, aliases, deprecations. |
| `shared` | pipeline | ETL adapter (driving, batch) | Helpers shared by the core ingest steps. |
| `snapshot_date` | pipeline | ETL adapter (driving, batch) | Snapshot date of the measurements load (measurements source). |
| `source_ingest_rules` | pipeline | pipeline rules | Source ingest rules (steps 01 and 02), domain: no file, no db. |
| `add_fact` | composition | composition root | Binds add_fact_service; the library API. |
| `container` | composition | composition root | The composition root of the whole application: one object that wires every use case for one `Locations`. |
| `docs` | composition | composition root | Composition root for the documentation generator: binds `docs_service` to the repository file adapter. |
| `entities` | composition | composition root | Composition root for entity command use cases. |
| `facts_batch` | composition | composition root | Binds facts_batch_service. |
| `lifecycle` | composition | composition root | Fact lifecycle (#8, #23): supersede, retract and set-visibility, each one appended revision. |
| `migrate_memory` | composition | composition root | Import Claude Code memory files as private, pending facts (issue #29). |
| `queries` | composition | composition root | Composition root for the read side: binds `queries_service` to the real read adapter (`fact_reads_store`). |
| `review` | composition | composition root | Binds review_service. |
| `rules_edit` | composition | composition root | Binds rules_edit_service; also the knowledge privacy commands' read access to the rules. |
| `scripts` | composition | composition root | Composition root for the script-backed commands: binds `scripts_service` to `script_runner`. |
| `subjects` | composition | composition root | Composition root for subject command use cases. |
| `wiring` | composition | composition helper | Composition helper: verify a service's dependencies, then publish its functions with them bound. |
| `acquisition_store` | infrastructure | infrastructure | Custodial history for sources and vault files (#46): when and how a file came into the collection. |
| `add_fact_store` | infrastructure | infrastructure | Locked JSON file, citekey/subject lookups. |
| `build_info_store` | infrastructure | infrastructure | Build info and an inputs manifest (#47). Library only; `knowledge.py build-info` wraps the reads. |
| `claims_store` | infrastructure | infrastructure | Claims audit SQL + clock. |
| `clock` | infrastructure | infrastructure | The one place that reads the wall clock for anything written into the data. |
| `docs_store` | infrastructure | infrastructure | Adapter for `DocsFiles`: reads and writes the repository's own docs and sources under one root directory. |
| `entities_store` | infrastructure | infrastructure | Entities as first-class rows (#42): the people, organizations, places, projects and substances facts are about. |
| `entity_migration_store` | infrastructure | infrastructure | Moving privacy keywords onto private entities (#42). Library only; `knowledge.py entity migrate-keywords` wraps it. |
| `entity_queries_store` | infrastructure | infrastructure | Read-only entity and subject queries for the CLI. |
| `fact_queries` | infrastructure | infrastructure | CLI read-only SQL. |
| `fact_reads_store` | infrastructure | infrastructure | One read-only connection per call over fact_queries / claims_store. |
| `fixity_store` | infrastructure | infrastructure | File fixity and identity for source files (#38). Library only; `knowledge.py audit-sources` wraps it. |
| `ids` | infrastructure | infrastructure | Random source keys. |
| `lifecycle_store` | infrastructure | infrastructure | Adapter for the fact lifecycle (#8, #23, #33): what the decisions need from the outside world. |
| `locks` | infrastructure | infrastructure | Inter-process file lock for the data-file writers. |
| `migrate_memory_store` | infrastructure | infrastructure | Memory files, run lock. |
| `modes_store` | infrastructure | infrastructure | Adapter for the mode rules (#22): the SQL that applies `modes` to the knowledge db. |
| `paths` | infrastructure | infrastructure | Reads where this person's data lives from the private data checkout, once, at an entry point. |
| `privacy_store` | infrastructure | infrastructure | Adapter for the privacy rules (driven side of the privacy domain). |
| `private_edit_store` | infrastructure | infrastructure | The shared edit-and-commit flow for hand-curated JSON files in knowledge-private (#43, #42). |
| `private_git` | infrastructure | infrastructure | Git around knowledge-private. |
| `review_store` | infrastructure | infrastructure | Pending rows, states. |
| `revisions_store` | infrastructure | infrastructure | Entry files, log file, lock, db. |
| `script_runner` | infrastructure | infrastructure | Starts the standalone scripts. |
| `source_status_store` | infrastructure | infrastructure | The fact-to-source audit SQL. |
| `subjects_store` | infrastructure | infrastructure | The subject hierarchy as data (#43). Library only; `knowledge.py subject ...` wraps the edits. |
| `add_fact_service` | application | application service | Add one fact. |
| `docs_service` | application | application service | Keep the generated regions of README.md and docs/\*.md in step with the code: `build` rewrites them, `check` reports drift. |
| `entities_service` | application | application service | Entity command use cases. All file and database work is supplied through ports. |
| `facts_batch_service` | application | application service | Batch add. |
| `lifecycle_service` | application | application service | Fact lifecycle (#8, #23): supersede, retract and set-visibility, each one appended revision. |
| `migrate_memory_service` | application | application service | Import Claude Code memory files as private, pending facts (issue #29). |
| `queries_service` | application | application service | Read side: search, list, show, history, audits, build info. |
| `review_service` | application | application service | Approve / reject. |
| `rules_edit_service` | application | application service | Edit privacy rules. |
| `scripts_service` | application | application service | Build and clean-concerts as a script run. |
| `subjects_service` | application | application service | Subject command use cases. File and database work is supplied through ports. |
| `add_fact_port` | domain_services | domain service | Port exposing the single-fact use case to batch and migration flows. |
| `claims_audit` | domain_services | domain service | Staleness rules. |
| `clock_port` | domain_services | domain service | Port for the application's clock. |
| `docs_files_port` | domain_services | domain service | Port for the files the documentation generator reads and writes: the docs, the two config files, and every module's source. |
| `docs_rules` | domain_services | domain service | Generated documentation, the rules: which doc regions are rendered from code, and how (domain: no file, no import of the code it describes). |
| `entity_files_port` | domain_services | domain service | Port for reading and editing persisted entity data. |
| `entity_migration_port` | domain_services | domain service | Port for migrating privacy keywords into private entities. |
| `entity_queries_port` | domain_services | domain service | Read port for entity links and subject hierarchy query results. |
| `fact_reads_port` | domain_services | domain service | Read-side port: facts, history, audits, build info (no connection). |
| `facts_batch_rules` | domain_services | domain service | Batch add_facts, the rules (domain: no file, db, git, clock or mode state). |
| `facts_file_port` | domain_services | domain service | Port for the persisted fact-entry file and its validation lookups. |
| `ids_port` | domain_services | domain service | Port for creating fresh source identifiers. |
| `lifecycle_reads_port` | domain_services | domain service | Read port for inputs used by lifecycle decisions. |
| `lifecycle_rules` | domain_services | domain service | Fact lifecycle (#8, #23, #33), the rules: result types and the pure decisions. |
| `memory_files_port` | domain_services | domain service | Port for discovering and reading source memory files. |
| `migrate_memory_rules` | domain_services | domain service | Memory-file rules. |
| `modes` | domain_services | domain service | Issue #22: the mode rules (domain), plus set_mode. The SQL that applies them is in `modes_store`. |
| `new_fact` | domain_services | domain service | A new fact, the rules (domain: no file, db, clock or randomness). |
| `privacy` | domain_services | domain service | Visibility rules + resolver. |
| `privacy_rules_port` | domain_services | domain service | Port for reading and editing the persisted privacy rules. |
| `private_git_port` | domain_services | domain service | Application boundary for Git operations on the private data repository. |
| `review_reads_port` | domain_services | domain service | Read port for the pending review queue and current fact states. |
| `review_rules` | domain_services | domain service | Review of pending facts (#6), the rules: result types and the pure decisions behind the queue. |
| `reviews_port` | domain_services | domain service | Port exposing review operations to lifecycle use cases. |
| `revisions` | domain_services | domain service | Fact revisions (#30): an append-only log of changes to facts, queryable as history. |
| `revisions_port` | domain_services | domain service | Port for persisted entries and their append-only revision log. |
| `script_runner_port` | domain_services | domain service | Port for running the standalone scripts as processes. |
| `subject_files_port` | domain_services | domain service | Port for reading and editing the persisted subject hierarchy. |
| `entity_rules` | model | model | The entity vocabulary and its error type: what the entities file may contain, free of how it is stored. |
| `fact_entry` | model | model | The fact entry and the revision record as types with behaviour. |
| `fact_rules` | model | model | Pure vocabulary shared by the fact-writing modules (domain: no I/O). |
| `fact_state` | model | model | A fact's lifecycle as types with behaviour: `Status`, `Visibility` and `FactState`. |
| `identifiers` | model | model | DOI / ISBN / ISSN / PMID / arXiv normalization (pure). |
| `locations` | model | model | Where this person's data lives, as one immutable value. |
| `source_status` | model | model | Source status, relation checks, replacement chains. |
| `textmatch` | model | model | Literal whole-word term matching, shared by the privacy keyword list (#31) and entity aliases (#42). |
| `timestamps` | model | model | Pure timestamp parsing for revision times (domain: no clock, no I/O). |
| `validtime` | model | model | Valid-time dates: parsing, ordering, containment. |
<!-- generated:module-map:end -->
