# Design proposal: `knowledge.db` as the source of truth

Status: **decisions made, nothing built.** The owner chose the recommended option for all four decisions in "Decisions to
settle" ((a) db first with a one-way export, (b) importer + verifier, (c) the revision-log/privacy model as proposed,
(d) local live db plus snapshots). The written design still needs the owner's sign-off before increment 1.

## Today

- The authoritative inputs are files in the private data directory (a git repo): fact files (`general_facts.json`,
  `facts_batch*.json`, `pilot_facts.json`), the append-only `fact_revisions.jsonl`, `subjects.json`, `entities.json`,
  `privacy_rules.json`, `manual_sources.json`, `fact_hints.json`, `claims.json`, `artist_rules.json`, plus the citation
  notes in the notes vault and the client inputs (concerts CSV, RYM export, scrobbles JSON, vault measurements db).
- `build_service.rebuild` rebuilds a whole new db in a temp file and swaps it in (backup first, newest `KEEP_BACKUPS`
  kept). Every write command (`add-fact`, `add-facts`, `supersede`, `privacy tag`, ...) edits a JSON file and makes a
  `private_git` commit; the change reaches the db only at the next rebuild. The MCP/CLI/inbox read the built db.
- Integer ids (`facts.id`, ...) are **not stable** across rebuilds; `facts.source_key` is the stable identity.
- The live db (26 MB, `journal_mode=delete`, no WAL) sits in a MEGA-synced folder, next to ~120 MB of backups.

## The db is not one kind of data

Rows in the live db (counts from the current db) fall into two groups, and the design differs for each:

| Group | Tables | Where the truth comes from today | After |
| --- | --- | --- | --- |
| **Curated** (made by hand or by Claude here) | `facts` (609), `fact_revisions`, `subjects`/`subject_aliases`, `entities`/`entity_aliases`/`fact_entities`, `fact_sources`, `claims`/`claim_facts`, privacy rules, manual `sources` | JSON files | **the db** |
| **Derived** (a copy of something outside) | `concert_attendances`, `albums`, `scrobbles` (153k), `measurements` (12k), citation-note `sources`/`vault_files` | CSV/JSON/sqlite/markdown outside the db | **still the outside file**; re-imported *incrementally* |

Only the curated group can become source of truth. The derived group must not be: nobody types in 153k scrobbles. The
catch: today a rebuild wipes everything, so it can refresh derived data freely. Once curated rows live only in the db,
the importers must **upsert into the live db** (key: natural keys such as `source_key`, citekey, scrobble timestamp)
and never drop curated rows.

## Design

### Writes
One write path, in the application ring: a use case opens a transaction on the live db, checks the rules it checks now
(`fact_state`, freshness, `validtime`, `modes.check_write`, the privacy floor), writes `facts` + a `fact_revisions` row
together, commits. The existing application services keep their shape; their *ports* change from "file store" to
"db store" (e.g. `facts_file_port` -> a `FactStore` port with a SQLite adapter). `private_git` is no longer in the write
path. Ids become stable, so the revision log can point at `fact_id` as well as `source_key`.

### Revision log and privacy
- `fact_revisions` becomes the log itself (today it is rebuilt from the jsonl). It is **append-only by trigger**, with one existing exception: the privacy floor may raise a revision's `visibility` to `private` (and only that; `audit_rules.revisions_guard_sql`). Hard-deleting a fact is refused, because the cascade would delete its history
  (`BEFORE UPDATE/DELETE` raise) and each write inserts the revision in the same transaction as the `facts` change, so
  state and history cannot diverge. `fact_revisions.jsonl` is retired as an input and appears only in the export.
- Privacy: `visibility` stays a stored column and the "anything unmarked is private" default stays. The privacy rules
  (keywords, tags, private entities) move to tables; the **privacy floor** runs inside each write transaction for the
  fact written and as a full sweep on `knowledge.py privacy check`. Private mode / normal mode / off (`modes`,
  `modes_store`) are read-side filters over the same db and do not change. `knowledge-normal.db` stays a *derived*
  file, regenerated (with the leak test) after any write that can change what it contains (a visibility change, an
  approval, a retraction, a rule change); a failure removes it, as now.
- Concurrency rule (see below): one writer at a time via the existing `locks` file lock, plus SQLite's own lock.

### The files
Depending on decision (a), the JSON files are either retired, a one-way export, or a mirror. The export is a
deterministic dump of the curated tables (stable ordering, one file per table, no timestamps of the dump itself) into
the private data repo, committed by `private_git` after each write or on demand (`knowledge.py export`).

### The rebuild pipeline
`ingest/` splits by group. Curated steps (`seed_general_facts`, `facts`, `seed_subject_hierarchy`, `link_entities`,
`seed_sources`, `apply_fact_revisions`, `seed_claims`) become either a one-time **import** (file -> db, used for
the migration and for restoring from an export) or are deleted. Derived steps (`literature_sources`, `client/concerts`,
`music_ratings`, `scrobbles`, `measurements`) stay, but become upserts into the live db. `build` keeps a role as a
**verifier**: rebuild curated tables from the export into a temp db and diff against the live db (restore drill), and
`build --check` already builds into a throwaway file and touches nothing live.

### History and backup
Git stops recording each change as a side effect of the write, so history needs an explicit home:
1. `fact_revisions` is the fact-level history (already exists, now append-only, complete for facts).
2. The git export gives line-level history of every curated table, one commit per write (or batched).
3. Point-in-time snapshots by the SQLite online backup API (`VACUUM INTO` / `Connection.backup`), never `cp` of a live
   file, with the existing keep-newest-N policy. Restore = copy a snapshot back, or import the last git export into a
   fresh db and re-run the derived importers. The restore procedure is a CLI command (`knowledge.py restore`) with a
   test that restores into a temp dir and compares.

### MEGA and SQLite
SQLite files in a sync folder can be corrupted by the sync client uploading a half-written file, or by two machines
writing. With `journal_mode=delete` (current) a crash mid-write leaves a `-journal` file; with WAL there is a `-wal` and
`-shm` that a sync client will copy out of step. Either way the **live, writable db should not be synced**. Rule: one
machine owns the writes; the synced folder only receives consistent snapshots and the read-only `knowledge-normal.db`.

## Decisions to settle

**(a) Direction of writes.**
1. *DB first, files are a one-way export (recommended).* Single truth, no drift; git history comes from the export.
2. *DB first, files are a two-way mirror.* Edit-by-hand stays possible but needs conflict rules; the drift we are trying
   to end comes back.
3. *DB only, no export.* Simplest, but loses git history and a human-readable backup.

**(b) The rebuild pipeline.**
1. *Importer + verifier (recommended).* Curated steps become import/restore; derived steps stay as upserts; `build`
   becomes the restore drill that diffs.
2. *Importer only.* Same, without the verify/diff command.
3. *Remove it.* Only valid if the derived sources are also dropped; not recommended.

**(c) Revision log and privacy.** Proposed: append-only `fact_revisions` by trigger, privacy rules in tables, floor
inside the write transaction, `knowledge-normal.db` regenerated after relevant writes. Alternative: keep the jsonl as
the authoritative log and the rest in the db (not recommended: two truths for one fact).

**(d) History, backup and where the live db sits.**
1. *Live db outside MEGA (local data dir), git export + online-backup snapshots into the synced folder (recommended).*
2. *Keep the live db in MEGA, add WAL-less single-writer lock, snapshots next to it.* Works only while one machine
   writes; MEGA can still capture a mid-write file.
3. *Live db local only, git export only, no snapshots.* Smaller, but restore needs a full import.

## Logging

Added after approval; the owner chose all three layers, built on our own SQL triggers and the standard library (no new
dependency). The owner also wants to **know when an entry is accessed**, so reads are logged too.

| Layer | What it records | Where |
| --- | --- | --- |
| **Row audit** | every insert/update/delete on a curated table: table, row key, old and new values (JSON), time, `via` (cli, mcp, inbox, import, restore), session id | `audit_log` table, written by `AFTER` triggers; append-only by trigger like `fact_revisions` |
| **Access log** | each time a fact is read through a driving adapter: `source_key`, time, `via`, session id, mode, and the kind of read (`show`, `history`, `search-hit`, `list`) | `access_log` table, written by the read use cases |
| **Operation log** | one record per command or run (write, import, export, snapshot, restore, build check, failure): name, arguments with secrets/statement text left out, outcome, duration | `operation_log` table |
| **Diagnostic log** | debugging detail from the CLI, MCP server and inbox | stdlib `logging`, JSON lines to a rotating local file next to the live db (not the synced folder) |

Decisions and constraints that follow:

- **Who** made a change: a trigger sees only the row, so the write use case sets the actor/session on its connection
  (a one-row `session_context` temp table the triggers read). A write without a context is recorded as `unknown`, never skipped.
- **Reads become writes.** Today every read opens its own read-only connection (`fact_reads_store`). Logging an access
  needs a writable one, so the read use cases get a separate `AccessLog` port whose adapter writes on its own short
  connection. A failure to log an access is reported (diagnostic log, a warning) but **does not fail the read**; a
  failed *audit* write is part of the write transaction and does fail it.
- **Search volume.** A search returns many entries; they are logged as one batched insert per query (kind `search-hit`),
  and `show` / `history` of one entry are the strong signal ("opened"). Retention is bounded: access rows older than a
  configured number of days are rolled up into a per-fact `last_accessed_at` / `access_count` summary table, never silently dropped.
- **Privacy.** All four logs are `private`: they are not in the normal-tier whitelist (`normal_rules`), and the leak test
  must fail if one appears in `knowledge-normal.db`. Normal mode reads still log, but only the keys of entries the
  caller was allowed to see, and the access log of a fact never reveals a private fact's existence to a normal-mode reader.
  Statement text is never copied into the operation or diagnostic logs (only keys), so a log file cannot leak a private fact.
- **CLI-first**: `knowledge.py log audit|access|ops [--fact KEY] [--since ...]` reads each log; every new use case has its command.
- **Prior art** for the trigger design: [sqlite-history-json](https://pypi.org/project/sqlite-history-json/) and
  [sqlite-diff-log](https://pypi.org/project/sqlite-diff-log/) (JSON audit rows from triggers, restore-to-entry); both
  note that triggers cannot see the actor, which the session-context table above addresses. Not adopted: alpha-stage and
  we need the append-only and privacy rules covered by our own tests.

## Prior art

Checked 2026-11-03 with two web searches (anecdotal sources; the sqlite.org pages were not fetched):

- **Diffable export of a SQLite db into git** is an established pattern: [sqlite-diffable](https://github.com/tirkarthi/sqlite-diffable)
  (Simon Willison's design: one JSON/NDJSON file per table plus metadata, "works great for less than a GB or so") and
  [sqlite2dir](https://github.com/rotty/sqlite2dir) (SQL schema plus JSON data, can commit each export to a git repo).
  sqlite2dir's author says an export is not a backup tool and points to SQLite's own `.backup`/`.dump`. This matches the
  split here: export for history, online-backup snapshots for restore. Our export must additionally be *importable*
  and byte-stable, so we write our own thin exporter over the curated tables rather than adopt either tool; the format
  choice (NDJSON per table, sorted by key) follows sqlite-diffable.
- **SQLite in a sync folder**: forum reports consistently say WAL's `-wal`/`-shm` files can sync out of order and corrupt the
  db, sync clients ignore SQLite locks, and the workaround is a local db with the Backup API copying into the synced
  folder (e.g. [a project that moved from WAL to TRUNCATE for this reason](https://github.com/foundry-9/quilltap-server/releases/tag/4.3.1),
  [Zotero forum thread](https://forums.zotero.org/discussion/comment/282766/)). This supports decision (d).
- Not looked at: Dolt (versioned SQL db) and Litestream (streaming replication). Both are heavier than a 26 MB single-user db;
  worth a note if the owner wants them considered.

## One backup folder (decided after 2b)

The first export (2b) covered only the curated tables, so a restore still needed the outside source files. The owner
wants one folder that is the whole backup, not an ingest from about 20 paths. Decided:

- The export covers **every data table**; only build provenance and the full-text index are skipped (rebuilt).
- **Two parts in one folder**: `export/core/` (curated tables; committed to the private repo after each write) and
  `export/bulk/` (client tables such as scrobbles, measurements and music; refreshed on demand, kept out of git or
  committed rarely, because it is large). One manifest per part; `restore` reads both.
- **Notes are opt-in and separate**: the citation-note markdown is copied into `export/notes/` only with a flag
  (`--include-notes`); the db keeps what was parsed from them either way.
- Client modules **register their own table lists** as data (the base never imports `client/`), like the build step list.
- The importers for the outside files become optional refreshes (increment 3).

Built (PR 95). Measured on the real data: `core/` 2.1 MB, `bulk/` 21 MB (2.6 MB gzipped; 153k scrobbles are most of it), `notes/` 2.0 MB (435 files). Export -> import -> export is byte-identical for core and bulk. Still open: whether `bulk/` goes into git (about 21 MB per changed refresh) or stays outside it; the default recommendation is outside git, with `core/` committed per write.

## Granular export (decided after 2b)

The owner does not want a fixed split to decide what is backed up: scrobbles and measurements are data like any other
once the db is the source of truth, and any subset should be exportable. Decided:

- `core/`, `bulk/` and `notes/` are only a folder layout. What is exported is a **selection**: `--table` and `--group`
  (repeatable) name tables or groups, `--skip` removes tables, naming nothing means everything the database has.
  `verify` takes the same selection. Groups: facts, sources, subjects, entities, claims, logs, and one per client
  source (its name and tables, from the client registry).
- Selection is literal: a group does **not** pull in the tables it references. Verify and import report dangling references.
- A manifest lists everything exported into its part so far, so partial exports accumulate in one folder; entries from
  another schema version are dropped.
- The logs (`audit_log`, `access_log`, `access_summary`, `operation_log`) are exportable and, when the database has them,
  part of the default export.
- `import` stays new-database-only and fails (creating nothing) if the export cannot stand alone. A separate
  `restore-tables` command (increment 2c) replaces chosen tables inside an existing database in one transaction, after a
  safety snapshot, and refuses if it would leave dangling references.
- Whether `bulk/` is committed to git is a per-run choice, not baked in: git growth is the compressed size of what changed
  (about 2.6 MB for a full scrobble rewrite, far less for appends), not the 21 MB on disk.

## Increments after approval (each independently mergeable, current pipeline untouched until step 4)

1. **Characterize.** Tests pinning every current write path (callers of `facts_file_port`, `revisions_port`, ...) and a
   golden export of today's data. No behavior change.
2a. **Log tables and triggers (done, `live.sql`, `audit_rules`, `live_schema_store`)**: kept out of `schema.sql` so the rebuild and its schema fingerprint are untouched; applied to the live database only. The `sources` table is not audited yet (mostly derived). 2b. **Deterministic export and import round trip (done: `knowledge.py db export|verify|import`, `export_rules`/`export_service`/`import_service`).** One NDJSON file per table plus `manifest.json` (row counts and SHA-256), default directory `export/` in the private data dir; checked on a copy of the live db: export -> import -> export is byte-identical (2.1 MB for 609 facts). Committing the export with `private_git` after each write comes with the write adapter in increment 4. 2c. **Snapshots, `restore` and `restore-tables` (done: `knowledge.py db snapshot|snapshots|restore|restore-tables`).** Snapshots use SQLite's online backup API into `snapshots/` beside the database (verified, never overwritten, newest 10 kept, only `snapshot-*.db` names touched). `restore` copies a snapshot to a NEW file; replacing the live db is done by moving the old one away yourself. `restore-tables` replaces chosen tables inside an existing database from a snapshot file or an export folder in one immediate transaction, after a safety snapshot, with `--dry-run`; it rolls back if it would introduce a foreign-key problem the database did not already have, and refuses the history tables (`fact_revisions`, `audit_log`, `operation_log`). The snapshot directory is derived from `db_dir` for now; the config key that moves it into the synced folder arrives with the flip to a local live db (increment 4). The rest of this item:
2. **Schema + stable ids + append-only triggers + `audit_log`/`access_log`/`operation_log` tables and triggers**, plus the deterministic export and `restore` (round-trip tested
   against the live data: file -> db -> export equals file).
2b. **Logging use cases**: `AccessLog` and operation-log ports and adapters, the diagnostic log, `knowledge.py log ...`, and the leak-test extension.
3. **Importers become upserts** for the derived group; curated steps become `import`.
4. **Flip the write ports** to a db adapter behind a config switch; a test scans callers so no write silently still
   goes to a file. Run both paths on a copy and diff.
5. **Retire** the file write path and `fact_revisions.jsonl` as an input; migrate the live data; docs and memory notes
   updated (the standing "never hand-edit the db" rule is replaced by "write only through the application services").

On hold until this is decided: the 52 California-election facts (`add-facts` writes `general_facts.json`).
Not touched: branch `claude/measurements-db-setting` and PR #90.
