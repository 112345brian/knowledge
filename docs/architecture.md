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
composition roots (review, lifecycle, add_fact, facts_batch, migrate_memory, rules_edit, entities, subjects)
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
model (fact_rules, timestamps, validtime, textmatch, identifiers, source_status, entity_rules, fact_state, fact_entry, locations, pipeline_context, fixity_rules, acquisition_rules, subject_rules)   <- imports nothing else
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
| maintenance tools (backfills, exports) | `DataFiles` (`datafiles_port`), `Inputs` | `local_datafiles`, `local_inputs` |
| `build.py`, `normal_db`, `leak_scan_store`, `leak_test` | `BuildFiles` (`build_files_port`) | `local_buildfiles` |
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

| Module | Role | What it is |
| --- | --- | --- |
| `acquisition_rules` | model | an acquisition record's vocabulary and validation (pure) |
| `build_files_port` | domain service | port: the disk and process work of a database rebuild |
| `cli_audits` | driving adapter | `audit-claims`, `build-info`, `audit-source-status`, `audit-sources` |
| `cli_build` | driving adapter | `build`, `clean-concerts` |
| `cli_common` | driving adapter | helpers and options shared by the knowledge.py command modules |
| `cli_facts` | driving adapter | `search`, `show`, `history`, `subjects`, `facts` |
| `cli_privacy` | driving adapter | `privacy ...` commands |
| `cli_review` | driving adapter | `review-pending`, `approve`, `reject` |
| `container` | composition root | wires every use case for one Locations; made by an entry point |
| `datafiles_port` | domain service | port: the data files a maintenance tool reads and rewrites |
| `fact_entry` | model | the fact entry and the revision record as types with behaviour (pure) |
| `fact_reads_port` | domain service | read-side port: facts, history, audits, build info (no connection) |
| `fact_reads_store` | infrastructure | one read-only connection per call over fact_queries / claims_store |
| `fact_state` | model | a fact's lifecycle: Status, Visibility, FactState (pure) |
| `fixity_rules` | model | a file fingerprint's vocabulary (pure) |
| `inputs_port` | domain service | port: what a build step reads from outside the database |
| `local_buildfiles` | infrastructure | the real disk, sqlite files and module loader (implements build_files_port) |
| `local_datafiles` | infrastructure | the real files, the atomic writer and the lock (implements datafiles_port) |
| `local_inputs` | infrastructure | the real file system, the vault db, fingerprints (implements inputs_port) |
| `local_stores` | infrastructure | the privacy, entity, subject and revision stores, by name (implements step_stores_port) |
| `locations` | model | where the data lives, as one immutable value (pure) |
| `pipeline_context` | model | what a build step is given besides its connection (a value, no I/O) |
| `queries` | composition root | binds queries_service to fact_reads_store |
| `queries_service` | application service | read side: search, list, show, history, audits, build info |
| `script_runner_port` | domain service | port for running the standalone scripts as processes |
| `scripts` | composition root | binds scripts_service to script_runner; a child is started for the container's checkout and fixed clock only |
| `scripts_service` | application service | build and clean-concerts as a script run |
| `seed_sources` | ETL adapter (driving, batch) | manual sources from `manual_sources.json` |
| `literature_sources` | ETL adapter (driving, batch) | citation notes in the notes vault, parsed into sources |
| `measurements` | ETL adapter (driving, batch) | numeric readings and lab results (measurements source) |
| `facts` | ETL adapter (driving, batch) | hand-authored pilot and batch fact files |
| `seed_claims` | ETL adapter (driving, batch) | claims citing the facts that back them (claims source) |
| `seed_subject_hierarchy` | ETL adapter (driving, batch) | subject parents, relation types, aliases, deprecations |
| `concerts` | ETL adapter (driving, batch) | concerts CSV export (concerts source) |
| `music_ratings` | ETL adapter (driving, batch) | ratings export (ratings source) |
| `scrobbles` | ETL adapter (driving, batch) | scrobble export (scrobbles source) |
| `seed_artist_members` | ETL adapter (driving, batch) | curated artist membership (music sources) |
| `seed_general_facts` | ETL adapter (driving, batch) | ad hoc facts from `general_facts.json` |
| `apply_fact_revisions` | ETL adapter (driving, batch) | validates and applies the append-only revision log |
| `link_entities` | ETL adapter (driving, batch) | loads entities and links facts to the entities they mention |
| `link_source_relations` | ETL adapter (driving, batch) | source-to-source relations: replaces, is-version-of |
| `shared` | ETL adapter (driving, batch) | helpers shared by the core ingest steps |
| `acquisition_store` | infrastructure | custodial history from macOS file attributes (read-only xattr) |
| `add_fact` | composition root | binds add_fact_service; the library API |
| `cli_add_fact` | driving adapter | `knowledge.py add-fact` (#7) |
| `add_fact_service` | application service | add one fact |
| `add_fact_store` | infrastructure | locked JSON file, citekey/subject lookups |
| `artist_rules` | pipeline rules | artist aliases |
| `backfill_dates` | ETL adapter (driving, batch) | one-time: `date_added` for legacy fact entries |
| `backfill_extracted_hashes` | ETL adapter (driving, batch) | one-off: records fixity baselines |
| `backfill_rules` | pipeline rules | key insertion into JSON text |
| `backfill_snapshot_date` | ETL adapter (driving, batch) | one-time: writes measurements_snapshot.json (measurements source) |
| `backfill_source_keys` | ETL adapter (driving, batch) | one-time: `source_key` for legacy fact entries |
| `build` | ETL adapter (driving, batch) | runs the steps, swaps the db |
| `build_info_store` | infrastructure | which inputs and code produced the db |
| `build_rules` | pipeline rules | step order, backups |
| `claims_audit` | domain service | staleness rules |
| `claims_store` | infrastructure | claims audit SQL + clock |
| `clean_concerts_csv` | ETL adapter (driving, batch) | idempotent cleanup of the concerts CSV (concerts source) |
| `cli_entities` | driving adapter | entity commands (#42) |
| `cli_facts_batch` | driving adapter | `knowledge.py add-facts` |
| `cli_inbox` | driving adapter | `knowledge.py inbox` and `edit` |
| `cli_lifecycle` | driving adapter | `knowledge.py supersede`, `retract`, `set-visibility` |
| `cli_migrate` | driving adapter | `knowledge.py migrate-memory` |
| `cli_parity` | driving adapter | CLI/serving parity registry (pure) |
| `cli_sources` | driving adapter | source ids (#48) |
| `cli_subjects` | driving adapter | subject commands (#43) |
| `clock` | infrastructure | time |
| `clock_port` | domain service | interface for time required by use cases |
| `add_fact_port` | domain service | interface exposing single-fact operations to batch callers |
| `entity_rules` | model | entity types and the entities error (pure) |
| `entities` | composition root | binds entity use cases to file, migration and query adapters |
| `entity_files_port` | domain service | interface for persisted entity data |
| `entity_migration_port` | domain service | interface for keyword migration |
| `entity_queries_port` | domain service | interface for entity and subject read queries |
| `entities_service` | application service | entity command operations |
| `entity_queries_store` | infrastructure | entity/subject CLI read queries |
| `entities_store` | infrastructure | entities.json: validation, linking, edits |
| `entity_migration_store` | infrastructure | keywords to private entities (two files at once) |
| `export_music_taste` | ETL adapter (driving, batch) | exports the music dataset as one JSON file (music sources) |
| `fact_ingest_rules` | pipeline rules | fact-file ingest rules |
| `facts_file_port` | domain service | interface for the persisted fact file and its lookups |
| `fact_queries` | infrastructure | CLI read-only SQL |
| `fact_rules` | model | allowed values |
| `facts_batch` | composition root | binds facts_batch_service |
| `facts_batch_rules` | domain service | batch item rules |
| `facts_batch_service` | application service | batch add |
| `fixity_store` | infrastructure | file fingerprints and the changed-since-extraction audit |
| `identifiers` | model | DOI / ISBN / ISSN / PMID / arXiv normalization (pure) |
| `ids` | infrastructure | random source keys |
| `ids_port` | domain service | interface for creating source identifiers |
| `inbox` | driving adapter | HTTP review page |
| `knowledge` | driving adapter | Typer app |
| `leak_rules` | pipeline rules | marker matching |
| `leak_scan_store` | ETL adapter (driving, batch) | scans normal DB files and derives private markers |
| `leak_test` | ETL adapter (driving, batch) | runs the scanner and fixture self-test (also a script) |
| `lifecycle` | composition root | binds lifecycle_service |
| `lifecycle_rules` | domain service | supersede/retract/visibility/edit decisions |
| `lifecycle_service` | application service | supersede / retract / set-visibility / edit |
| `lifecycle_store` | infrastructure | rules + subject tree for the floor |
| `lifecycle_reads_port` | domain service | interface for inputs used by lifecycle decisions |
| `measurement_rules` | pipeline rules | vault rows to measurements |
| `measurements_port` | domain service | interfaces for the measurement writes and the fact <-> measurement links |
| `measurements_service` | pipeline use case | load the vault's readings, link facts to measurements, through ports (measurements source) |
| `measurements_store` | infrastructure | SQLite measurement repository and fact-link queries |
| `link_fact_measurements` | ETL adapter (driving, batch) | links facts to the measurements they cite (measurements source) |
| `locks` | infrastructure | inter-process file lock for the data-file writers |
| `migrate_memory` | composition root | binds migrate_memory_service |
| `migrate_memory_rules` | domain service | memory-file rules |
| `migrate_memory_service` | application service | memory migration |
| `migrate_memory_store` | infrastructure | memory files, run lock |
| `memory_files_port` | domain service | interface for memory-file operations |
| `modes` | domain service | mode policy, write gating |
| `modes_store` | infrastructure | mode-filtered fact queries |
| `music_shared` | ETL adapter (driving, batch) | artist identity helpers for the music source |
| `music_ingest_rules` | pipeline rules | concerts / RYM / Last.fm rules |
| `new_fact` | domain service | NewFact + validation + build_entry |
| `normal_db` | ETL adapter (driving, batch) | builds the normal db from a full db (SQL over the connections it is given, files through a BuildFiles; also a script) |
| `normal_rules` | pipeline rules | normal-tier whitelist |
| `paths` | infrastructure | personal paths |
| `step_stores_port` | domain service | port: the data files and table writers a build step uses |
| `subject_rules` | model | subjects as values: vocabulary, validation, pure edits |
| `wiring` | composition helper | `bind_all` / `verify`: checks adapters against their ports, publishes a service's functions bound |
| `privacy` | domain service | visibility rules + resolver |
| `privacy_store` | infrastructure | rules file, db apply |
| `privacy_rules_port` | domain service | interface for reading and editing privacy rules |
| `private_edit_store` | infrastructure | load-edit-write-commit flow for hand-curated files |
| `private_git` | infrastructure | git around knowledge-private |
| `private_git_port` | domain service | protocol for operations application code needs from the private-data Git adapter |
| `revisions_port` | domain service | interface for entries and the revision log |
| `review_reads_port` | domain service | interface for review queue and fact-state reads |
| `reviews_port` | domain service | interface exposing review operations to lifecycle |
| `review` | composition root | binds review_service |
| `review_rules` | domain service | review decisions |
| `review_service` | application service | approve / reject |
| `review_store` | infrastructure | pending rows, states |
| `revisions` | domain service | entries, keys, log format, next revision |
| `revisions_store` | infrastructure | entry files, log file, lock, db |
| `rules_edit` | composition root | binds rules_edit_service; also the knowledge privacy commands' read access to the rules |
| `rules_edit_service` | application service | edit privacy rules |
| `script_runner` | infrastructure | starts the standalone scripts |
| `snapshot_date` | ETL adapter (driving, batch) | snapshot date and configuration of the measurements load (measurements source) |
| `source_ingest_rules` | pipeline rules | citation notes |
| `source_status` | model | source status, relation checks, replacement chains |
| `source_status_store` | infrastructure | the fact-to-source audit SQL |
| `subjects` | composition root | binds subject use cases to file and query adapters |
| `subjects_service` | application service | subject command operations |
| `subjects_store` | infrastructure | subjects.json: hierarchy as data, aliases, edits |
| `subject_files_port` | domain service | interface for persisted subject data |
| `textmatch` | model | whole-word term matching |
| `timestamps` | model | revision-time parsing |
| `validtime` | model | valid-time dates: parsing, ordering, containment |
| `vault_reader_port` | domain service | interface for reading the vault's database as typed records |
| `vault_reader_store` | infrastructure | SQLite reader of the vault's database |
