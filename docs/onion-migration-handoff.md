# Onion migration: integration handoff

**Goal.** Finish the onion migration of this repo: steps stop holding SQL, DB handles or dynamic loading, and
services own decisions behind typed ports. The flat layout stays. The package-reorganization branch
(`codex/clean-readme-private-data`) stays out.

## Where it stands

Branch `claude/onion-integration` is base `claude/pure-onion-architecture` (PR #57) plus the twelve lane PRs merged
locally in dependency order, with every conflict resolved. It is green: pytest 2304 passed and 1 skipped (hermetic,
no private checkout), `tach check` clean, 10 of 10 import-linter contracts kept, mypy clean.

| Lane | PR | In this branch |
|---|---|---|
| Foundation contracts | #66 | yes |
| Exports and maintenance | #68 | yes |
| Core editing | #61 | yes |
| Claims | #60 | yes |
| Sources and relations | #62 | yes |
| Subject hierarchy | #58 | yes |
| Facts ingestion | #67 | yes |
| Revisions, privacy, entity linking | #64 | yes |
| Measurements and fact links | #70 | yes |
| Music ingestion | #59 | yes |
| Normal database and leak checks | #69 | yes |
| Build and provenance | #63 | yes |
| Architecture and acceptance gates | #65 | **no** |

The lane PRs themselves are unchanged and still open against `claude/pure-onion-architecture`; this branch is the
integrated result, not a replacement for reviewing them.

## What integrating changed beyond conflict resolution

- `subject_hierarchy_service.subject_for_fact` has an explicit `resolve_aliases` flag. Fact files never resolved
  aliases; general facts do. #58 had made it always resolve.
- The facts service reaches subject and vault-file identity through `fact_ingest_port.Identity`, answered by
  `fact_ingest.BuildIdentity` over `subject_hierarchy_service` and `source_ingest_service`. Use cases may not import
  each other, so the composition root delegates.
- Skip warnings in the facts ingest are emitted as found (`warn=print`), so a failing build keeps them.
- `StepResult` is `build_steps_port`'s only; the music lane's own copy is gone.
- The per-run artist cache is `artist_directory.ArtistDirectory`, shared by `identity_service` and
  `music_ingest_service`. The identity legacy-definition pin in `tests/test_identity_service.py` is empty.
- `measurements_service.link_facts` is now `link_fact_measurements` (the entity-link guard in #64 scans by name).
- `container.build_use_case(outputs, files=...)` runs the registry and session factory over the given `files`, so
  the foundation's step tracer sees every step. `remove_db_files` lives once, in `build_rules`.

## Open items

1. **Parallel ports (the main one).** No lane adopted the foundation's `build_repositories_port`. Sources, subjects,
   facts, music, normal-db and build each defined their own port, so two contracts exist for the same aggregates.
   Target: the foundation's contract (it has the unit of work, the id-allocation ordering contract the golden dump
   depends on, fakes and characterization tests). Keep narrowness by having each service declare a small Protocol of
   only the methods it calls. Do it one aggregate at a time with `tests/golden/pipeline_dump.json` as the check.
   Some lane ports also put decisions in adapters (conflict detection in `add_source`, alias rules in a get-or-create);
   those move to the service.
   - **Sources: done.** `sources_port` keeps only `SourceReader`. `source_repository_store.SqliteSourceRepository`
     now implements `build_repositories_port.SourceRepository` and decides nothing; `source_ingest_service` maps a
     `SourceRecord` to `NewSource` and uses `identity_service.SourceIdentity` for identifier conflicts and vault-file
     fixity (a test scans that nothing else writes identifiers). The pipeline dump is byte-identical before and after.
     The foundation port had no production adapter before this; `identity_service` had no production caller.
   - Remaining aggregates, same recipe: subjects, facts, music, normal-db, build.
     Compare dumps with `tests/golden/pipeline_dump.json` (on Windows it differs in four path tables, so diff a dump
     taken before the change against one taken after).
2. **#65 (gates) is not merged.** Its premise is "no exemptions"; merge it last and expect it to flag what the port
   collapse removes. Report, don't loosen.
3. ~~`canonical_artist_name` / `artist_key` in `identity_rules`~~ Done: defined once in `client/artist_rules.py`;
   `artist_directory` is their only caller. `identity_rules.artist_rules_from_mapping` still builds a second
   `ArtistRules` (`build_records`) beside `client.artist_rules.ArtistRules`: another parallel definition for the port collapse.
4. ~~`remove_db_files` duplicated~~ Done: one definition in `build_rules`, taking the file port's `remove_if_exists`.
5. **Real private checkout (partly run).** `build --check` and a real build into a scratch DB, against a copy of
   `knowledge-private`, compared table by table with `origin/main`. Found and fixed: facts, manual sources and
   literature notes had started storing the *expanded* vault path (`C:/Users/x/MEGA/...`) instead of the path as
   `local_paths.py` writes it (`~/MEGA/...`), and 13 facts lost their origin vault file because the expanded vault
   no longer matched the `~/...` text in their `notes`. `Locations` now carries `notes_vault_written` /
   `health_dir_written` (`*_stored` properties). The hermetic suite missed it because every fixture vault is
   absolute. Remaining differences from main are timestamps/commit and main's own Windows mojibake (`â€”`).
   Not yet run: the real MEGA inputs (see the OS-agnostic paths task).
   The private data also needs `ingest.backfill_dates --apply` and `client.backfill_snapshot_date --apply` before
   any build passes, on main as well; the private clone itself has not been modified.
6. **PR #57's body** lacks the "Generated with Claude Code" footer; PR #56 (`claude/generated-docs`) is unrelated
   but may conflict on README and docs.

## Verify

```bash
uv run pytest -q && uv run tach check && uv run python tests/arch_check.py && uv run mypy
```

## Next step

Decide how this lands: one integration PR from this branch into `claude/pure-onion-architecture`, or the lane PRs
rebased individually. Then merge #65 last and start the port collapse as separate PRs.
