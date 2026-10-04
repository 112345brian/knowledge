# knowledge

This repo (`~/programming/knowledge`) is the pipeline: `schema.sql` + the
numbered ingest/seed scripts + the CLI. It contains **no personal data and
no personal file paths** — those live in a private companion repo,
`knowledge-private`, expected checked out as a sibling directory
(`../knowledge-private` relative to this one):

- `knowledge-private/local_paths.py` — every real external path (the health
  vault, the concerts export, the RYM export, the scrobbles export, and
  where `knowledge.db` itself lives). This repo's own `paths.py` is a thin
  loader that imports from there; it carries zero personal path strings.
- `knowledge-private/data/` — the actual fact/source content: hand-authored
  sources (`manual_sources.json`), interpretive facts (`pilot_facts.json`,
  `facts_batch1-4.json`), and ad hoc facts (`general_facts.json`, appended
  to via `add_fact.py`).

Without `knowledge-private` checked out alongside it, this repo won't run —
that's intentional, not a bug: it's meant to show the pipeline design, not
to be runnable standalone by anyone who clones it.

`knowledge.db` itself lives at the path `local_paths.py` designates
(currently inside the MEGA-synced folder, deliberately outside *both* git
repos, so a sync client never touches either repo's `.git` internals, but
the built artifact still ends up synced). `python3 knowledge.py build`
writes a timestamped backup there before every rebuild as a safety net,
since git isn't tracking the db file's history (it isn't in either repo).

`knowledge.db` is a **build artifact** of the scripts here plus the data
files in `knowledge-private`. Never hand-edit the `.db` file with ad hoc
`ALTER`/`INSERT` statements — change a script and rebuild.

## CLI

`knowledge.py` is the one entry point — everything below also still runs as
its own standalone script if you'd rather call it directly.

```bash
python3 knowledge.py build            # rebuilds knowledge.db in place (auto-backs up the old file first)
python3 knowledge.py build --check    # builds into a throwaway temp file and reports counts; live DB untouched
python3 knowledge.py add-fact "statement" --subject some-subject --trust medium   # see add_fact.py --help for all flags
python3 knowledge.py clean-concerts   # dedupes the concerts export in place
python3 knowledge.py search "terms" [--subject x] [--trust high] [--personal-only|--not-personal]
python3 knowledge.py show <fact_id>
python3 knowledge.py subjects
python3 knowledge.py facts [--subject x] [--trust high] [--status active]
```

## Pipeline order

1. `schema.sql` — canonical DDL: every table, index, view, FTS5 virtual table + triggers.
2. `01_seed_sources.py` — loads `knowledge-private/data/manual_sources.json`: hand-authored sources (DEXA scans, bloodwork panels, a few literature pilots, small manual instruments) that don't come from parsing vault frontmatter or a vault db.
3. `02_ingest_literature_sources.py` — mechanically parses the vault's `sources/*.md` frontmatter files into `sources` rows.
4. `03_ingest_measurements.py` — pulls every structured numeric reading from the vault's own db: DEXA, bloodwork, tape, strength checkpoints, then daily/weekly summaries and the full raw logs (per-set training, per-meal/per-food nutrition, Fitbit, micronutrients). Creates its own `vault-db-*` sources for the raw tables it reads.
5. `04_ingest_facts.py` — loads `knowledge-private/data/pilot_facts.json` (hand-authored facts) and `facts_batch1-4.json` (facts extracted by background agents reading the vault's synthesis and harm-reduction notes) into `facts`/`fact_sources`/`fact_measurements`. Classifies `is_personal` with a documented heuristic and resolves `origin_path` from each fact's `notes` field.
6. `05_seed_claims.py` — hand-authored broader claims and which facts back them.
7. `06_seed_subject_hierarchy.py` — arranges `aas-*` subjects under `anabolic-steroids` and `training-*` subjects under a new `training` umbrella.
8. `11_seed_general_facts.py` — loads `knowledge-private/data/general_facts.json`: ad hoc facts with no project or vault behind them, added one at a time via `add_fact.py` rather than in a batch. Defaults new subjects to `domain='general'` instead of `health-and-fitness`.

## Adding new data

- **New literature citation**: drop a note in the vault's `sources/` folder with the usual frontmatter, then rebuild — `02_ingest_literature_sources.py` picks it up automatically.
- **New DEXA scan / lab panel / logged data**: update the vault's own db (its own pipeline handles that), then rebuild — `03_ingest_measurements.py` re-reads it. Note this makes `measurements` a snapshot as of the last rebuild, not a live view; rerun `build.py` after new vault data lands if you want it reflected here.
- **New interpretive fact from reading more of the vault**: this step still needs a human/agent to actually read the source file and produce a JSON object matching the shape used in `facts_batch*.json` (see `04_ingest_facts.py` for the exact fields), added to `knowledge-private/data/`. Extraction prompts used for the existing batches are preserved in this session's transcript / the `project_knowledge_db` memory note, not in either repo.
- **New original claim/decision with no vault file behind it**: add it to `knowledge-private/data/pilot_facts.json` (or a new manually-curated JSON file there) with an explicit `origin_path` (or `null` if it's genuinely un-sourced), then rebuild.
- **A stray, not-project-specific fact** (nothing to do with bodybuilding or any other vault — just something worth recording): `python3 add_fact.py "statement" --subject some-subject --trust medium` appends a validated entry to `knowledge-private/data/general_facts.json` (see `add_fact.py --help` for the full flag set: notes, recheck-by/rationale, an existing source citekey, marking it not-personal, etc.), then rebuild. Don't hand-edit that JSON directly — the CLI enforces the shape `11_seed_general_facts.py` expects.
- **Dates**: `add_fact.py` stamps each new entry's `date_added` (ISO-8601 UTC with offset, from `clock.py`) and ingest stores it, so it never changes on rebuild. Entries without one (the original 295) fall back to a fixed, documented `LEGACY_DATE_ADDED` constant in `04`/`11`. Tests freeze the clock with `clock.frozen(...)` or `KNOWLEDGE_FROZEN_NOW`.
- **Provenance**: optional `captured_via` (`cli`, `mcp`, `migrate-memory`, ...), `session_id`, `captured_at` (stamped from `clock.py`) and `source_quote` on `facts`. `add_fact.py` (`--captured-via`, `--session-id`, `--source-quote`) requires `session_id` and `source_quote` when `captured_via` is `mcp`; a quote may then stand without a `--source-citekey`. Plain CLI adds need none of it. `knowledge.py show` does not print these yet.
- **Claims and the stale-premise audit (#1)**: `claims` has a nullable `inference_type` (`deductive`, `inductive`, `abductive`), set on new claims only; the 2 existing claims are deliberately not backfilled (NULL = unclassified). A claim's premises are its `claim_facts`. `claims_audit.audit_claims(db, today=None)` lists every (claim, premise) where the fact is `superseded`, `retracted`, or still active but past an ISO-date `recheck_by` (`reason` = `superseded` / `retracted` / `past_recheck_by`); `v_claims_with_stale_premises` is the SQL view for the status cases. `recheck_by` text that is not an ISO date is never flagged; `unparseable_rechecks(db)` lists it. The `knowledge.py audit-claims [--json]` command is not wired yet (it waits for the Typer migration, #34); until then call `claims_audit.audit_claims` directly. Nothing in the pipeline can set `superseded`/`retracted` yet (#8), so against the real data it returns no rows.
- **Auto-commit of knowledge-private (git as backup and safety net)**: `add_fact.py` commits its change to `knowledge-private` itself (message `add-fact: <subject> (<trust_level>)`), so every mutation is already in git history and the tree is clean afterward. That is what makes git a reliable backup and safety net for the private data; the primary, queryable fact history is the revision log (#30), not git. Before writing, it refuses (exit 1, nothing written) if `knowledge-private` has any uncommitted change, staged, unstaged or untracked, so a commit never bundles unrelated edits; `--allow-dirty` skips that check for a deliberate batch edit (the commit still contains only the facts file). If the commit itself fails (hook, no git identity, ignored file) the fact is in the JSON but uncommitted, the error says so, and the exit code is 3. The repo is found from the data file's directory (so `KNOWLEDGE_PRIVATE_DIR` works); a data dir outside any git repo is written without committing and a note says so. It never pushes. Future mutating commands (`approve`, `supersede`, `retract`, `set-visibility`) reuse `private_git.py`: `find_repo(dir)`, `ensure_clean_tree(repo)` before writing, `commit_private_change([path], message, repo)` after, with `PrivateGitError` for failures.
- **Schema change**: edit `schema.sql` directly (it's the definition, not a migration diff), then rebuild.
- **New external path an ingest script needs**: add it to `knowledge-private/local_paths.py`, never inline it in a script here.

## Known caveats

- `sqlite3` (the CLI) on this machine is not compiled with FTS5. Full-text search (`facts_fts`, `sources_fts`) works from Python's `sqlite3` module — use `python3 knowledge.py search "..."` — or a client like DB Browser for SQLite / Datasette, but not from the bare `sqlite3` command line.
- `measurements` from the raw vault logs (44K+ rows) are a snapshot as of the last rebuild, not a live-synced view — the vault's own db keeps growing as you log new sets/meals, this doesn't.
- `is_personal` is a heuristic classification (pronoun/fingerprint/file-origin signals), not hand-verified per fact — see `classify_is_personal()` in `04_ingest_facts.py`. **It is not a privacy boundary**: the facts it marks not-personal include sensitive material (health, for one). Use `facts.visibility` for anything about who may see a fact.
- `facts.visibility` is `private` or `normal` (`schema.sql` CHECK, indexed). Every fact is `private` unless its JSON entry says `"visibility": "normal"`; it is never derived from `is_personal`. `add_fact.py --visibility normal` sets it (default `private`, invalid values are rejected). Nothing filters on it yet.
- **Privacy rules (#31, `privacy.py`)**: the stored `visibility` is computed, never chosen by the model: the most restrictive of (1) what the caller asked for, (2) the `private` tag on the fact's subject or any ancestor (subjects form a tree via `parent_id`), and (3) a keyword/name list matched on whole words, case-insensitively (`brother` does not match `brotherhood`; punctuation and hyphens are boundaries; rules are literal text, never regexes). A caller can only raise visibility. A subject that does not exist yet is `private` (fail closed) whenever the subject list is known. The result explains itself (`Resolution.explain()` names the rule). **This is word matching plus subject tags, not semantic detection**: nicknames, pronouns and unlisted names are missed, which is why human review stays. Rules live in `privacy_rules.json` in `PRIVATE_DATA_DIR` (knowledge-private; it holds names, so it is never in this repo), format `{"version": 1, "subject_tags": {"family": "private", "new-topic": "normal"}, "keywords": ["..."]}` (a `normal` tag just registers a subject as known; it never lowers). **No rules file means empty rules** (no tags, no keywords); a corrupt or non-object file is an error that fails the build and refuses `add_fact`. `add_fact.append_fact` stores the resolved value (and returns `AddResult.privacy`); steps 04 and 11 re-apply the current rules to every fact at the end (`privacy.apply_rules_to_db`, raise-only, also sets `subjects.private`), so adding a rule privatizes old facts on rebuild, while removing a rule never downgrades a fact stored `private`. Tag inheritance needs `parent_id`, which step 06 sets, so step 11 (last) is the pass that sees the whole tree. CLI commands for `privacy check`/tag/keyword are wired separately (#34); the library functions are `check`, `tag_subject`, `untag_subject`, `add_keyword`, `remove_keyword`, `load_rules`, `save_rules`.
