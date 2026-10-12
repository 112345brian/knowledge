# Acceptance: what each build step does today

Observed behaviour of the legacy `run(con, ctx)` steps, in run order (`build_rules.PIPELINE`; client steps run
only when their source is enabled, the relative order is the same either way). It is pinned by
`tests/test_build_characterization.py` (order, commits, skip rules, output, failures) and
`tests/test_pipeline_golden.py` (rows). A migrated step is accepted when it produces the same lines, the same
commit pattern and the same rows. Oddities marked **kept** are part of the behaviour: preserve them, fix them
only as a separate, deliberate change.

## Build level (`build.build`, not a step)

* Order: `schema.sql` + enabled fragments (`client/music.sql` for any music source, `client/measurements.sql`),
  `PRAGMA foreign_keys = ON` first; then `build_rules.steps_for(sources)`; then `build_info`; `VACUUM`; `ANALYZE`.
  A failure in schema, a step, or build_info is a `BuildError(stage)` with stage `schema`, the step path
  (`ingest/facts.py`), or `build_info`; only `Exception` is caught.
* One connection for the whole build. Steps commit themselves; nothing rolls back. A failed build discards the
  temp file, so rows a failing step wrote but did not commit are never seen (`tests/test_build.py` covers the
  live database staying byte-identical).
* Client source configuration errors (`config`) are raised before any file is created.

## Steps

`Commits` = commit calls the step issues. "Output" lines are printed after the last write unless marked *warn*
(printed at the moment it is raised).

| # | Step | Reads | Skip / early-exit rules | Commits | Output |
| --- | --- | --- | --- | --- | --- |
| 1 | `ingest/seed_sources` | `manual_sources.json` (required); `{VAULT}`/`{HEALTH}` placeholders in `origin_path` need those locations or the step raises | none; a duplicate citekey raises | 1, at the end | *warn* duplicate identifier; `[seed_sources] inserted N manual sources` |
| 2 | `ingest/literature_sources` | `<vault>/sources/*.md`, sorted, `README.md` skipped | no `NOTES_VAULT`: parses nothing; a citekey already in `sources` is skipped but counted as parsed | 1 | *warn* duplicate identifier; `[literature_sources] parsed P files, inserted I new sources` |
| 3 | `client/measurements` | `measurements_snapshot.json` (required, first, before any write), `NOTES_VAULT/bodybuilding.db`, `measurements_config.json` | missing snapshot/vault/config raises before writing; a null bloodwork mapping is skipped; only the latest strength row is used | 1, after all inserts | `[measurements] inserted N measurement rows (S subjects, M metrics touched)` / `  plus ...` |
| 4 | `ingest/facts` | `pilot_facts.json`, `facts_batch1..4.json`, `privacy_rules.json` (loaded first: a corrupt file fails early), `fact_hints.json` | `Skip` from `screen_item` -> warning, counted skipped; a duplicate `source_key` raises ValueError | 1, after the privacy pass | *warn* skip warnings; optional `  privacy rules raised N fact(s) to private`; `[facts] inserted I, skipped S (bad shape)`; then, in this order, unknown-citekey warning, legacy-key note, three tallies |
| 5 | `client/link_fact_measurements` | the same fact files via `facts.load_items` | no metric link -> ignored; fact missing (skipped earlier) -> ignored | 1 | `[link_fact_measurements] N fact <-> measurement links` |
| 6 | `client/seed_claims` | `claims.json` | absent file -> no claims | 1 | `[seed_claims] inserted N claims` |
| 7 | `ingest/seed_subject_hierarchy` | `subjects.json` | file absent -> `[seed_subject_hierarchy] no subjects.json: no subject hierarchy loaded`, **no commit** | 1 (0 when skipped) | `[seed_subject_hierarchy] N subjects now have a parent (subjects.json)` / `  A aliases, D deprecated subjects` |
| 8 | `client/concerts` | `CONCERTS_CSV` (`utf-8-sig`), `artist_rules.json` | row without name/date -> dropped silently | 1 | `[concerts] inserted N concert_attendances` + totals, unresolved-festival, one `billing=` line per billing in first-seen order |
| 9 | `client/music_ratings` | `RYM_EXPORT_CSV`, `artist_rules.json` | unusable row -> dropped | 1 | `[music_ratings] inserted N albums` / `  artists: A` |
| 10 | `client/scrobbles` | `SCROBBLES_JSON`, `artist_rules.json` | file missing -> `[scrobbles] <path> not found, skipping`, **no writes, no commit** | 1 (0 when skipped) | `[scrobbles] parsed P scrobbles, A new artists, T new tracks` / totals |
| 11 | `client/seed_artist_members` | `artist_rules.json` | none | 1 | `[seed_artist_members] inserted N artist_members rows` |
| 12 | `ingest/seed_general_facts` | `general_facts.json`, `privacy_rules.json` | as facts; subjects resolve through aliases, new ones default to domain `general`; `is_personal` defaults to 1 | 1, after the privacy pass | `[seed_general_facts] inserted I, skipped S (bad shape)`; unknown-citekey warning; legacy-key note |
| 13 | `ingest/apply_fact_revisions` | `fact_revisions.jsonl` (absent = empty), `privacy_rules.json` | none | **2** (inside `apply_revisions`, then after the floor and the history UPDATE) | optional `  privacy rules raised N fact(s) to private after revisions`; `[apply_fact_revisions] applied N revisions (T rows in fact_revisions incl. implicit revision 1)` |
| 14 | `ingest/link_entities` | `entities.json` | absent -> `[link_entities] no entities.json; no entities`, **no commit**; a fact mentioning a private entity but not stored private raises before the commit | 1 (0 when skipped) | `[link_entities] E entities (P private), L fact links` |
| 15 | `ingest/link_source_relations` | manual sources + note frontmatter (re-parsed by loading the two step modules) | none; no relations is fine | 1, always | `[link_source_relations] N relations; sources by status: {...}` |

## Kept behaviour a lane must not "fix" in passing

* `literature_sources` writes the literal `retrieved_date` `'2026-09-11'`, ignoring the parsed value.
* `apply_fact_revisions` commits twice (the store commits inside `apply_revisions`).
* `facts` tallies print `is_personal` with int keys (`{0: 6, 1: 2}`) and string keys otherwise, in value order.
* `link_source_relations` and `link_fact_measurements` reach into other steps (`seed_sources.load_sources`,
  `literature_sources.parse_all`, `facts.load_items`): a migrated version needs those readers extracted, not
  duplicated.
* A subject cache and an artist cache are per step run; `measurements` reports how many names it touched.
* `entities_store.link_facts` holds the matching rules (`match_all`) next to its SQL; the entity lane should
  split decision from persistence the same way as the others.
