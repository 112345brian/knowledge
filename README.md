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

`knowledge.py` is the one entry point, a [Typer](https://typer.tiangolo.com) app (`--help` on it and on every command; shell completion via `--install-completion`). `build.py`, `add_fact.py` and `clean_concerts_csv.py` still run standalone with plain `python3` and do not need typer.

**Setup (once):** the CLI and the tests need typer and pytest, declared in `pyproject.toml` and installed into a repo-local `.venv` (git-ignored) by [uv](https://docs.astral.sh/uv/). Nothing goes into the system python.

```bash
uv sync                                        # creates .venv with typer + pytest
export KNOWLEDGE_PRIVATE_DIR=~/programming/knowledge-private   # as before
uv run pytest                                  # run the tests (plain `python3 -m pytest` fails: no typer)
uv run python knowledge.py --help
```

```bash
uv run python knowledge.py build            # rebuilds knowledge.db in place (auto-backs up the old file first)
uv run python knowledge.py build --check    # builds into a throwaway temp file and reports counts; live DB untouched
uv run python knowledge.py add-fact "statement" --subject some-subject --trust medium   # forwards every argument to add_fact.py; see add_fact.py --help
uv run python knowledge.py clean-concerts   # dedupes the concerts export in place
uv run python knowledge.py search "terms" [--subject x] [--trust high] [--personal-only|--not-personal] [--limit N] [--json]
uv run python knowledge.py show <fact_id> [--json]
uv run python knowledge.py subjects [--json]
uv run python knowledge.py facts [--subject x] [--trust high] [--status active] [--personal-only|--not-personal] [--limit N] [--json]
```

`--json` on a read command prints the same rows the library function returns (`search_facts`, `get_fact`, `list_subjects`, `list_facts`). An invalid full-text query (unbalanced quote, empty string) prints `error: search failed: ...` and exits 1.

**CLI first (parity rule).** Everything doable from a UI button, the inbox or an MCP tool is also a CLI command. For every new action, in this order: (1) a library function in `knowledge.py` (or its own module): pure, importable, returns data, never prints or exits; (2) a Typer command that only parses flags and prints, with `--json` if it reads; (3) the MCP tool / inbox handler, which calls the same library function. Register the action in `cli_parity.py` (`ACTIONS`: action name -> CLI command path). `mcp_server.py` and `inbox.py` must declare `PARITY_ACTIONS` (the action names they expose); `tests/test_cli_parity.py` fails if a registered action has no real command, or if either module exposes a name with no registry entry.

## Pipeline order

1. `schema.sql` — canonical DDL: every table, index, view, FTS5 virtual table + triggers.
2. `01_seed_sources.py` — loads `knowledge-private/data/manual_sources.json`: hand-authored sources (DEXA scans, bloodwork panels, a few literature pilots, small manual instruments) that don't come from parsing vault frontmatter or a vault db.
3. `02_ingest_literature_sources.py` — mechanically parses the vault's `sources/*.md` frontmatter files into `sources` rows.
4. `03_ingest_measurements.py` — pulls every structured numeric reading from the vault's own db: DEXA, bloodwork, tape, strength checkpoints, then daily/weekly summaries and the full raw logs (per-set training, per-meal/per-food nutrition, Fitbit, micronutrients). Creates its own `vault-db-*` sources for the raw tables it reads.
5. `04_ingest_facts.py` — loads `knowledge-private/data/pilot_facts.json` (hand-authored facts) and `facts_batch1-4.json` (facts extracted by background agents reading the vault's synthesis and harm-reduction notes) into `facts`/`fact_sources`/`fact_measurements`. Classifies `is_personal` with a documented heuristic and resolves `origin_path` from each fact's `notes` field.
6. `05_seed_claims.py` — hand-authored broader claims and which facts back them.
7. `06_seed_subject_hierarchy.py` — arranges `aas-*` subjects under `anabolic-steroids` and `training-*` subjects under a new `training` umbrella.
8. `11_seed_general_facts.py` — loads `knowledge-private/data/general_facts.json`: ad hoc facts with no project or vault behind them, added one at a time via `add_fact.py` rather than in a batch. Defaults new subjects to `domain='general'` instead of `health-and-fitness`.
9. `12_apply_fact_revisions.py` — reads `knowledge-private/data/fact_revisions.jsonl` (the revision log, see below), validates it and writes `fact_revisions` plus each fact's current state into `facts`. Runs last among the fact steps; a missing or empty log is fine.

## Fact revisions (#30)

A fact entry is never edited. The JSON entry is **revision 1**; every later change appends one line to `knowledge-private/data/fact_revisions.jsonl` (`{source_key, revision, changed_at, changed_via, session_id, change_reason}` plus a full snapshot of `statement, trust_level, trust_rationale, status (pending|active|superseded|retracted), visibility, superseded_by (a source_key), recheck_by, recheck_rationale, volatility, notes`, in that fixed key order). `facts` is still a real table holding the *current* state (the latest revision); `fact_revisions` holds every revision including the implicit revision 1 (`changed_at` comes from the entry's `date_added`, `changed_via` `original`).

- **`source_key`**: the stable identity a revision points at. `add_fact.py` gives every new fact one (`f-<12 hex>`). Entries that predate it get a deterministic `legacy-<10 hex>` key derived from file name + subject + statement (`revisions.derive_keys`; later duplicates of identical content get `-2`, `-3`). The build derives these in memory when an entry has none, so it works before the backfill; `backfill_source_keys.py` writes the *same* keys into the files, so building before or after yields identical rows. Revisions may be appended before or after the backfill.
- **Backfill (one-time, in-place edit of the legacy JSON files)**: `python3 backfill_source_keys.py` is a dry run; `--apply` writes. It inserts `"source_key"` as the first key of each entry that lacks one and changes nothing else (every other byte is preserved and the result is verified), under the same lock and atomic replace as `add_fact.py`; running it again changes nothing. Use `--data-dir` to try it on a copy. Commit the result in `knowledge-private` afterward.
- **Library API (`revisions.py`)**: `append_revision(source_key, changes, reason, via, session_id=None)` returns a `RevisionResult(ok, errors, revision)` (it refuses instead of raising, so check `ok`; a test fails if any caller discards the result); `get_history(db, fact_id | source_key)` lists every revision in order; `get_fact_as_of(db, fact_id | source_key, "YYYY-MM-DD")` returns the revision in force at the end of that day (UTC), or the exact moment for a full timestamp, `None` if the fact did not exist yet. `db` is a connection or a path. The `history` and `show --as-of` CLI commands are not wired yet (they follow the Typer migration, #34).
- **Validation at build time** (fails the build naming `fact_revisions.jsonl:<line>`): revision numbers contiguous per fact starting at 2 (a gap or duplicate fails), `changed_at` not before the previous revision, every `source_key` exists, `superseded_by` resolves, every line well-formed. `append_revision` runs the same checks first, so it will not write a line the build would reject. Blank lines are ignored. Hand-editing an old line is caught only by these checks and by git.
- `facts.status` now also allows `pending` (revisions and entries may set it; the approve workflow is #6).

## Adding new data

- **New literature citation**: drop a note in the vault's `sources/` folder with the usual frontmatter, then rebuild — `02_ingest_literature_sources.py` picks it up automatically.
- **New DEXA scan / lab panel / logged data**: update the vault's own db (its own pipeline handles that), then rebuild — `03_ingest_measurements.py` re-reads it. Note this makes `measurements` a snapshot as of the last rebuild, not a live view; rerun `build.py` after new vault data lands if you want it reflected here.
- **New interpretive fact from reading more of the vault**: this step still needs a human/agent to actually read the source file and produce a JSON object matching the shape used in `facts_batch*.json` (see `04_ingest_facts.py` for the exact fields), added to `knowledge-private/data/`. Extraction prompts used for the existing batches are preserved in this session's transcript / the `project_knowledge_db` memory note, not in either repo.
- **New original claim/decision with no vault file behind it**: add it to `knowledge-private/data/pilot_facts.json` (or a new manually-curated JSON file there) with an explicit `origin_path` (or `null` if it's genuinely un-sourced), then rebuild.
- **A stray, not-project-specific fact** (nothing to do with bodybuilding or any other vault — just something worth recording): `python3 add_fact.py "statement" --subject some-subject --trust medium` appends a validated entry to `knowledge-private/data/general_facts.json` (see `add_fact.py --help` for the full flag set: notes, recheck-by/rationale, an existing source citekey, marking it not-personal, etc.), then rebuild. Don't hand-edit that JSON directly — the CLI enforces the shape `11_seed_general_facts.py` expects.
- **Dates**: `add_fact.py` stamps each new entry's `date_added` (ISO-8601 UTC with offset, from `clock.py`) and ingest stores it, so it never changes on rebuild. Entries without one (the original 295) still fall back to a fixed, documented `LEGACY_DATE_ADDED` constant in `04`/`11` until `backfill_dates.py` has been run (#35): `python3 backfill_dates.py` is a dry run, `--apply` inserts `"date_added"` (the value the constant stands for: 2026-09-11, or 2026-09-26 for `general_facts.json`) as the first key of each entry that lacks one, plus a `measurements_snapshot.json` (`{"synced_at": "2026-09-11"}`) in the data dir if absent. Same safety properties as `backfill_source_keys.py` (byte-preserving, verified, locked and atomic, idempotent, never overwrites an existing value; a present-but-null or non-ISO `date_added` is reported and blocks all writes); `--data-dir` tries it on a copy. Commit the result in `knowledge-private` afterward. Tests freeze the clock with `clock.frozen(...)` or `KNOWLEDGE_FROZEN_NOW`.
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
