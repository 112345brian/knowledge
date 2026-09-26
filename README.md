# knowledge

This repo (`~/programming/knowledge`) is code only: `schema.sql` + the
numbered ingest/seed scripts + `data/`. **`knowledge.db` itself lives
elsewhere**, at `~/MEGA/library/knowledge/knowledge.db` (with its `backups/`
alongside it) — deliberately outside this repo, so the db and its history
stay synced by MEGA without a sync client ever touching this repo's `.git`
internals. That split location is `KNOWLEDGE_DB_DIR` in `paths.py`; every
script that opens the live db (`build.py`, `add_fact.py`, `knowledge.py`)
reads it from there. Never hardcode either location elsewhere.

`knowledge.db` is a **build artifact** of the scripts in this repo plus the
checked-in data files under `data/`. It is derived from
`<BODYBUILDING_VAULT>/` (the vault: markdown notes + its own
`bodybuilding.db`) and `<CONCERTS_CSV>`. Never
hand-edit the `.db` file with ad hoc `ALTER`/`INSERT` statements — change a
script here and rebuild. Every external path an ingest script reads from
(the vault, the concerts CSV, the RYM export, the scrobbles export, and the
db's own location) is centralized in `paths.py` — never inline one in a
script.

`python3 knowledge.py build` still writes a timestamped backup to
`backups/` before every rebuild as a safety net, since git isn't tracking
the db file's history (it isn't even in this repo).

## CLI

`knowledge.py` is the one entry point — everything below also still runs as
its own standalone script if you'd rather call it directly.

```bash
python3 knowledge.py build            # rebuilds knowledge.db in place (auto-backs up the old file first)
python3 knowledge.py build --check    # builds into a throwaway temp file and reports counts; live DB untouched
python3 knowledge.py add-fact "statement" --subject some-subject --trust medium   # see add_fact.py --help for all flags
python3 knowledge.py clean-concerts   # dedupes concerts.csv in place
python3 knowledge.py search "terms" [--subject x] [--trust high] [--personal-only|--not-personal]
python3 knowledge.py show <fact_id>
python3 knowledge.py subjects
python3 knowledge.py facts [--subject x] [--trust high] [--status active]
```

## Pipeline order

1. `schema.sql` — canonical DDL: every table, index, view, FTS5 virtual table + triggers.
2. `01_seed_sources.py` — hand-authored sources: the 6 original pilot citations (DEXA scans, Labcorp, 3 literature pilots) plus 3 small manual instruments (tape measurements, the strength-checkpoint script, the deliberately-unlocatable CRP/ESR carry-forward).
3. `02_ingest_literature_sources.py` — mechanically parses all 434 `sources/*.md` frontmatter files in the vault into `sources` rows.
4. `03_ingest_measurements.py` — pulls every structured numeric reading from the vault's `bodybuilding.db`: DEXA (both scans, full field set), bloodwork, tape, strength checkpoints, then daily/weekly summaries and the full raw logs (per-set training, per-meal/per-food nutrition, Fitbit, micronutrients). Creates its own `vault-db-*` sources for the raw tables it reads.
5. `04_ingest_facts.py` — loads `data/pilot_facts.json` (6 hand-authored facts) and `data/facts_batch1-4.json` (289 facts extracted by background agents reading the vault's 27 top-level synthesis notes and 31 harm-reduction files) into `facts`/`fact_sources`/`fact_subjects`/`fact_measurements`. Classifies `is_personal` with a documented heuristic and resolves `origin_path` from each fact's `notes` field.
6. `05_seed_claims.py` — the 2 hand-authored broader claims and which facts back them.
7. `06_seed_subject_hierarchy.py` — arranges `aas-*` subjects under `anabolic-steroids` and `training-*` subjects under a new `training` umbrella.
8. `11_seed_general_facts.py` — loads `data/general_facts.json`: ad hoc facts with no project or vault behind them, added one at a time via `add_fact.py` rather than in a batch. Defaults new subjects to `domain='general'` instead of `health-and-fitness`.

## Adding new data

- **New literature citation**: drop a note in the vault's `sources/` folder with the usual frontmatter, then rebuild — `02_ingest_literature_sources.py` picks it up automatically.
- **New DEXA scan / lab panel / logged data**: update `bodybuilding.db` (the vault's own pipeline handles that), then rebuild — `03_ingest_measurements.py` re-reads it. Note this makes `measurements` a snapshot as of the last rebuild, not a live view; rerun `build.py` after new vault data lands if you want it reflected here.
- **New interpretive fact from reading more of the vault**: this step still needs a human/agent to actually read the source file and produce a JSON object matching the shape used in `data/facts_batch*.json` (see `04_ingest_facts.py` for the exact fields). Add it to a new `data/facts_batchN.json` (or a differently-named batch file) and register it in `04_ingest_facts.py`'s `load_items()`, then rebuild. Extraction prompts used for the existing batches are preserved in this session's transcript / the `project_knowledge_db` memory note, not in this repo.
- **New original claim/decision with no vault file behind it**: add it to `data/pilot_facts.json` (or a new manually-curated JSON file) with an explicit `origin_path` (or `null` if it's genuinely un-sourced), then rebuild.
- **A stray, not-project-specific fact** (nothing to do with bodybuilding or any other vault — just something worth recording): `python3 add_fact.py "statement" --subject some-subject --trust medium` appends a validated entry to `data/general_facts.json` (see `add_fact.py --help` for the full flag set: notes, recheck-by/rationale, an existing source citekey, marking it not-personal, etc.), then rebuild. Don't hand-edit `data/general_facts.json` directly — the CLI enforces the shape `11_seed_general_facts.py` expects.
- **Schema change**: edit `schema.sql` directly (it's the definition, not a migration diff), then rebuild.

## Known caveats

- `sqlite3` (the CLI) on this machine is not compiled with FTS5. Full-text search (`facts_fts`, `sources_fts`) works from Python's `sqlite3` module — use `python3 knowledge.py search "..."` — or a client like DB Browser for SQLite / Datasette, but not from the bare `sqlite3` command line.
- `measurements` from the raw vault logs (44K+ rows) are a snapshot as of the last rebuild, not a live-synced view — `bodybuilding.db` keeps growing as you log new sets/meals, this doesn't.
- `is_personal` is a heuristic classification (pronoun/fingerprint/file-origin signals), not hand-verified per fact — see `classify_is_personal()` in `04_ingest_facts.py`.
