# CONTRACTS: the build's ports, records and ownership

Foundation for moving the build steps from `run(con, ctx)` (a raw sqlite connection plus SQL in the step) to
steps bound to typed ports. Nothing here migrates a step: the legacy pipeline still runs unchanged, and
[build-acceptance.md](build-acceptance.md) plus `tests/test_build_characterization.py` pin what a migrated step
must keep. The module/layer registration for these files (tach, import-linter, the module map in
architecture.md, the pinned lists in `tests/test_architecture.py`) is a separate final commit.

## Ownership split

| Concern | Owner | Where |
| --- | --- | --- |
| What a step decides: skip an item, validate, normalize, which subject/source/artist a name means, what to print | application and domain rules | the step itself, `identity_service`, `identity_rules`, `*_ingest_rules`, `measurement_rules`, `revisions`, `privacy`, ... |
| What persistence is asked to do, in the knowledge base's words | ports | `build_repositories_port` (build database), `build_steps_port.VaultReader` (vault database), `inputs_port.Inputs` (files) |
| The values exchanged | plain frozen records | `build_records` (plus the model's `Subject`, `Entity`, `privacy.Rules`) |
| SQL, files, fingerprints, the sqlite connection and its transaction | infrastructure (the adapters a lane adds) | `*_store`, `local_*`, a `SessionOpener` |
| Which adapter backs which port; one session per build; step order and binding | composition root | `build_composition` |
| The ingest order, schema, build_info, printing, `BuildError`, swapping the file | pipeline driver | `build.py`, `build_rules` |

The rule of thumb for a lane: if a line of the legacy step is an `if`, a loop condition, a message or a name
choice, it stays in the step (or moves to a rule); if it is a SELECT/INSERT/UPDATE or a file read, it becomes a
port call and the SQL moves into the adapter unchanged.

## Ports

All in `build_repositories_port.py`; every method takes and returns `build_records` values or identifiers, and
none executes SQL, hands out a cursor or a row. `BuildSession` holds one of each plus `unit_of_work`.

| Port | Operations (summary) | Legacy code it replaces |
| --- | --- | --- |
| `SourceRepository` | `citekeys`, `find`, `add(NewSource)` (publisher get-or-create inside), identifier owner/add/conflict, `link_author`, vault-file lookup/add/fixity, `add_relation`, `count_by_status` | `ingest/shared.py`, SQL in `seed_sources`, `literature_sources`, `link_source_relations`, `client/measurements.get_source_id` |
| `SubjectRepository` | `find_by_name`, `find_by_alias`, `add(name, domain?)`, `ensure`, `ids_by_name`, `set_hierarchy`, `set_replacement`, `add_alias` (raises `AliasRejected`), counts, `read_file` | the three `get_or_create_subject`, `seed_subject_hierarchy.run_entries` |
| `FactRepository` | `source_key_exists`, `find_by_source_key`, `add(NewFact)`, `link_source`, `link_measurements`, `tally(by)` | SQL in `facts`, `seed_general_facts`, `link_fact_measurements` |
| `RevisionRepository` | `insert`, `fact_ids_by_source_key`, `first_revisions`, `read_log`, `write_back`, `count` | `revisions_store.SqliteRevisionRepository` (`insert_revision_row` / `apply_revisions` remain as thin callers) |
| `PrivacyRepository` | `load_rules`, `apply_rules -> PrivacyOutcome`, `raise_history_of_private_facts` | `privacy_store.SqlitePrivacyRepository` (`load_rules` / `apply_rules_to_db` remain as thin callers) |
| `EntityRepository` | `read_file`, `add(Entity)`, `fact_texts`, `link_fact`, `fact_visibility` | `entities_store.read_file` / `link_facts` |
| `MeasurementRepository` | `find_metric`, `add_metric`, `add`, `exercise`/`food`/`muscle` (get-or-create), `add_training_set`, `add_food_log_entry`, `add_meal_log_entry`, `add_muscle_volume`, `totals` | SQL in `client/measurements` |
| `ClaimRepository` | `add`, `link_facts_by_prefix` | SQL in `client/seed_claims` |
| `MusicRepository` | `import_source`, `delete_*`, `artists`, `add_artist`, `venue`, `festival`, `add_attendance`, `add_album`, `tracks`, `add_track`, `add_scrobbles`, `add_artist_member`, `totals` | SQL in `concerts`, `music_ratings`, `scrobbles`, `music_shared` |
| `UnitOfWork` | `commit()` | `con.commit()` |
| `VaultReader` / `VaultOpener` (`build_steps_port`) | one method per vault table the measurements step reads | `inputs.open_database(...)` + `SELECT * FROM ...` |

Adapter obligations (an adapter that breaks one fails the characterization or golden test):

* Allocate ids in call order, one row per `add`/get-or-create call (the golden dump compares ids).
* `add(NewSource)` get-or-creates the publisher *before* inserting the source; authors are linked by separate
  `link_author` calls after identifiers, as the steps do.
* `apply_rules` and the repositories never commit; `revision_apply_service` commits through the `UnitOfWork`, once after the
  log is applied and once after the privacy floor and the history raise (see acceptance doc: `apply_fact_revisions`
  commits twice).
* A document reader (`read_file`, `load_rules`, `read_log`) returns `None`/empty for an absent file and raises for
  a corrupt one, exactly like the store it delegates to.

## Steps: `run() -> StepResult`

`build_steps_port`:

* `StepResult(step, lines, counts, skipped)`: `lines` are the output lines in print order, with exactly one
  `[step] ...` line; `counts` are the named tallies behind them.
* `Diagnostics.emit(line)`: a warning raised *while the step works* goes here immediately (so a step that later
  fails has already shown it). Everything after the step's last write is `StepResult.lines`; the runner emits
  them after `run()` returns. Together that reproduces today's print order exactly.
* `StepEnvironment(locations, inputs, diagnostics, vault=None)` is all a step factory gets besides the session.
* `StepError(stage, cause)` has `build.BuildError`'s attributes and message shape; the driver maps one to the
  other.

A step is a frozen `*Deps` of only the ports it uses plus a class with `run()`. The template is
`ExampleDeps` / `ExampleStep` in `tests/test_build_contracts.py`.

`build_composition`:

* `BuildSessionFactory(opener).open(target)` yields a `BuildSession`; the `SessionOpener` (infrastructure, added
  by the lane or integration owner) connects, applies the schema, sets `foreign_keys`, and closes.
* `PipelineBinder(registry).bind(session, paths, environment)` binds `build_rules.steps_for(sources)` in that
  order. A path with no registry entry raises `UnboundSteps` (a partial pipeline is never built silently), so a
  lane registers its factory and the driver switches over when every step is bound.
* `run_pipeline(bound, emit)` runs in order, emits each result's lines, wraps `Exception` as `StepError(path,
  cause)`, and returns the results (a test fails if a caller discards them).

## Frozen identity operations

`identity_service` (stateful, per step run) over `identity_rules` (pure). There is exactly one definition of each;
`tests/test_identity_service.py` pins the semantics and ratchets the legacy copies down as lanes delete them.

| Identity | Operation | Frozen behaviour |
| --- | --- | --- |
| Source (citekey) | `SourceIdentity.citekeys()` | snapshot of citekey -> id when called |
| | `.claims().claim(ck)` | False if the citekey is known, else remember it and return True (the notes step's `existing` set) |
| | `.require(ck, hint)` | id, or `SourceNotFound` (a `RuntimeError`): `source citekey not found: K (hint)` |
| | `.attach_identifiers(...)` | an identifier another source owns -> conflict row + warning, never merged; the same source repeating one is silent |
| | `.link_authors(...)` | `'; '`-split, stripped, blanks dropped but they still occupy a byline position |
| | `.vault_file(path, inputs)` | fingerprinted and acquisition-filled once per build; `None` for an empty path; unreadable -> `missing`, no raise |
| Subject | `SubjectIdentity.resolve(name, domain=None, via_aliases=False)` | exact name; alias table only if `via_aliases`; else create with `domain` or the schema default; cache by given name; `touched` = distinct names |
| Artist | `ArtistIdentity(music, rules)` | key = `lower()` of the unescaped, alias-mapped name; cache loaded once, first row of a case-variant group wins, created artists added, never evicted; `len()` = distinct keys; `seed_members()` returns new links |

## What a lane does

1. Pick the step(s); read their rows in [build-acceptance.md](build-acceptance.md).
2. Write a `*Deps` + step class; decisions stay, SQL becomes port calls; use the identity classes instead of the
   local helper and delete the helper's entry from `LEGACY_DEFINITIONS`.
3. Implement the adapter methods you need (SQL moved unchanged) in infrastructure.
4. Prove it: `tests/step_trace.py` traces the legacy step; run your step on the same world and compare `lines`,
   `commits`, `writes_after_last_commit`; `tests/test_pipeline_golden.py` compares the rows. Unit-test the step on
   `tests/fake_build_repositories.py` (see its docstring for what the fakes do and do not model).
5. Do not edit tach/pyproject/architecture.md registration; the integration owner rebases those.
