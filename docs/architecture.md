# Architecture map (ports and adapters)

The repo is a hexagon in flat modules. The domain is pure; use cases reach the world only through the port
protocols in `ports.py`; adapters implement the ports; the facades are the composition roots that bind a use
case to its adapters; the CLI, the inbox and the ingest scripts are driving adapters. The boundaries are
enforced mechanically (`tach.toml`, the import-linter contracts in `pyproject.toml`, and
`tests/test_architecture.py` / `tests/test_ports.py`), and `tests/test_pipeline_golden.py` pins what the
ingest pipeline produces. `tach.toml` carries the same layering as its `layers`: `serving`, `cli`, `pipeline` (the
driving adapters), then `facade`, `adapter`, `use_case`, `domain`, outermost first, and a test checks that every
module's layer there agrees with its role in the table below. This table is checked by `tests/test_architecture.py`: every module must appear
exactly once, and the *domain* and *use case* rows must equal the contract lists.

```
driving adapters (knowledge, cli_*, inbox, build + the `ingest` package)
        |  call
        v
facades (review, lifecycle, add_fact, facts_batch, migrate_memory, rules_edit) <- composition roots: pick the adapters
        |  bind
        v
use cases (*_service)  --uses-->  ports (ports.py)  <--implemented by--  driven adapters (*_store, clock, paths, private_git, ...)
        |  apply
        v
domain (pure rules: privacy, modes, revisions, new_fact, ... *_rules)
```

Rules (each one is a failing test or contract if broken):

1. Domain modules import no `sqlite3`/`os`/`pathlib`/`subprocess`/`uuid`/..., no adapter, no `clock`/`paths`/`private_git`
   (contract `domain-has-no-infrastructure`; the list can grow, never shrink).
2. Use cases (`*_service`) import ports and domain only, never an adapter, a facade or infrastructure
   (contract `use-cases-depend-on-ports`; `os` only for `os.path` string handling, checked by an AST test).
3. The CLI and the inbox import no database, process or file-system module and no write-side adapter
   (contract `driving-adapters-no-infrastructure`).
4. Only `private_git` runs git and only `script_runner` spawns a Python subprocess; nothing above the domain
   imports upward (layers: serving > cli > pipeline > library).
5. Every adapter module satisfies the protocol the facade binds it to (`tests/test_ports.py`).

| Module | Role | What it is |
| --- | --- | --- |
| `seed_sources` | ETL adapter (driving, batch) |  |
| `literature_sources` | ETL adapter (driving, batch) |  |
| `measurements` | ETL adapter (driving, batch) |  |
| `facts` | ETL adapter (driving, batch) |  |
| `seed_claims` | ETL adapter (driving, batch) |  |
| `seed_subject_hierarchy` | ETL adapter (driving, batch) |  |
| `concerts` | ETL adapter (driving, batch) |  |
| `music_ratings` | ETL adapter (driving, batch) |  |
| `scrobbles` | ETL adapter (driving, batch) |  |
| `seed_artist_members` | ETL adapter (driving, batch) |  |
| `seed_general_facts` | ETL adapter (driving, batch) |  |
| `apply_fact_revisions` | ETL adapter (driving, batch) |  |
| `link_entities` | ETL adapter (driving, batch) |  |
| `link_source_relations` | ETL adapter (driving, batch) |  |
| `shared` | ETL adapter (driving, batch) |  |
| `acquisition_store` | driven adapter | custodial history from macOS file attributes (read-only xattr) |
| `add_fact` | facade (composition root) | binds add_fact_service; also the add_fact.py script |
| `add_fact_service` | use case | add one fact |
| `add_fact_store` | driven adapter | locked JSON file, citekey/subject lookups |
| `artist_rules` | domain | artist aliases |
| `backfill_dates` | ETL adapter (driving, batch) |  |
| `backfill_extracted_hashes` | ETL adapter (driving, batch) | one-off: records fixity baselines |
| `backfill_rules` | domain | key insertion into JSON text |
| `backfill_snapshot_date` | ETL adapter (driving, batch) | one-time: writes measurements_snapshot.json (measurements source) |
| `backfill_source_keys` | ETL adapter (driving, batch) |  |
| `build` | ETL adapter (driving, batch) | runs the steps, swaps the db |
| `build_info_store` | driven adapter | which inputs and code produced the db |
| `build_rules` | domain | step order, backups |
| `claims_audit` | domain | staleness rules |
| `claims_store` | driven adapter | claims audit SQL + clock |
| `clean_concerts_csv` | ETL adapter (driving, batch) |  |
| `cli_entities` | driving adapter | entity commands (#42) |
| `cli_facts_batch` | driving adapter |  |
| `cli_inbox` | driving adapter |  |
| `cli_lifecycle` | driving adapter |  |
| `cli_migrate` | driving adapter |  |
| `cli_parity` | driving adapter | CLI/serving parity registry (pure) |
| `cli_sources` | driving adapter | source ids (#48) |
| `cli_subjects` | driving adapter | subject commands (#43) |
| `clock` | driven adapter | time |
| `entities_store` | driven adapter | entities.json: validation, linking, edits |
| `entity_migration_store` | driven adapter | keywords to private entities (two files at once) |
| `export_music_taste` | ETL adapter (driving, batch) |  |
| `export_subjects` | ETL adapter (driving, batch) | writes subjects.json from the seed hierarchy |
| `fact_ingest_rules` | domain | fact-file ingest rules |
| `fact_queries` | driven adapter | CLI read-only SQL |
| `fact_rules` | domain | allowed values |
| `facts_batch` | facade (composition root) | binds facts_batch_service |
| `facts_batch_rules` | domain | batch item rules |
| `facts_batch_service` | use case | batch add |
| `fixity_store` | driven adapter | file fingerprints and the changed-since-extraction audit |
| `identifiers` | domain | DOI / ISBN / ISSN / PMID / arXiv normalization (pure) |
| `ids` | driven adapter | random source keys |
| `inbox` | driving adapter | HTTP review page |
| `knowledge` | driving adapter | Typer app |
| `leak_rules` | domain | marker matching |
| `leak_test` | driven adapter | reads db files (also a script) |
| `lifecycle` | facade (composition root) | binds lifecycle_service |
| `lifecycle_rules` | domain | supersede/retract/visibility/edit decisions |
| `lifecycle_service` | use case | supersede / retract / set-visibility / edit |
| `lifecycle_store` | driven adapter | rules + subject tree for the floor |
| `measurement_rules` | domain | vault rows to measurements |
| `link_fact_measurements` | ETL adapter (driving, batch) | links facts to the measurements they cite (measurements source) |
| `locks` | driven adapter | inter-process file lock for the data-file writers |
| `migrate_memory` | facade (composition root) | binds migrate_memory_service |
| `migrate_memory_rules` | domain | memory-file rules |
| `migrate_memory_service` | use case | memory migration |
| `migrate_memory_store` | driven adapter | memory files, run lock |
| `modes` | domain | mode policy, write gating |
| `modes_store` | driven adapter | mode-filtered fact queries |
| `music_shared` | ETL adapter (driving, batch) | artist identity helpers for the music source |
| `music_ingest_rules` | domain | concerts / RYM / Last.fm rules |
| `new_fact` | domain | NewFact + validation + build_entry |
| `normal_db` | driven adapter | SQL copy + file swap (also a script) |
| `normal_rules` | domain | normal-tier whitelist |
| `paths` | driven adapter | personal paths |
| `ports` | domain | port protocols the use cases depend on (pure); counted as domain |
| `privacy` | domain | visibility rules + resolver |
| `privacy_store` | driven adapter | rules file, db apply |
| `private_edit_store` | driven adapter | load-edit-write-commit flow for hand-curated files |
| `private_git` | driven adapter | git around knowledge-private |
| `review` | facade (composition root) | binds review_service |
| `review_rules` | domain | review decisions |
| `review_service` | use case | approve / reject |
| `review_store` | driven adapter | pending rows, states |
| `revisions` | domain | entries, keys, log format, next revision |
| `revisions_store` | driven adapter | entry files, log file, lock, db |
| `rules_edit` | facade (composition root) | binds rules_edit_service; also the knowledge privacy commands' read access to the rules |
| `rules_edit_service` | use case | edit privacy rules |
| `script_runner` | driven adapter | starts the standalone scripts |
| `seed_rules` | domain | seed data |
| `snapshot_date` | ETL adapter (driving, batch) |  |
| `source_ingest_rules` | domain | citation notes |
| `source_status` | domain | source status, relation checks, replacement chains |
| `source_status_store` | driven adapter | the fact-to-source audit SQL |
| `subjects_store` | driven adapter | subjects.json: hierarchy as data, aliases, edits |
| `textmatch` | domain | whole-word term matching |
| `timestamps` | domain | revision-time parsing |
| `validtime` | domain | valid-time dates: parsing, ordering, containment |
