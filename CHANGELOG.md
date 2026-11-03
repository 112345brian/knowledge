# Changelog

Versions follow [Semantic Versioning](https://semver.org); while the major version is 0, a minor bump may break
compatibility, and each such change is listed under **Upgrading**.

<!-- towncrier release notes start -->

## 0.2.1 - 2026-11-03

### Added

- Releases are cut with towncrier and bump-my-version: each change adds a fragment to `changelog.d/`, a release compiles them into `CHANGELOG.md`, and one command bumps the version in every file that carries it, commits and tags. See `docs/releasing.md`.

### Changed

- Tests now fail when a relative link in the README or docs is broken, when `docs/README.md` does not list a document, or when `CHANGELOG.md` has no entry for the current version.

### Documentation

- The README gained sections on backup, export and restore and on using the knowledge base from Claude (the MCP server, `mcp_mode`, the skills), a Releases section, the full list of gates and CI, and project-layout rows for the 0.2 modules.
- `docs/architecture.md` has a table of every enforced gate and what it catches, and the I/O table covers the export, snapshot and MCP adapters; `docs/mcp.md` lists `mcp_moded_store`; `docs/README.md` indexes every document; the onion handoff note is marked historical.


## 0.2.0 - 2026-11-03

### Upgrading from 0.1.0

This release changes how the project is configured. Nothing is read from `local_paths.py` any more.

1. Create `~/.config/knowledge/config.toml` (`$XDG_CONFIG_HOME` is honored; the same path on every OS), or point
   `KNOWLEDGE_CONFIG` at a file. The README's "Configuration" section has an example. `db_dir` and `data_dir` are
   required; unknown keys are an error.
2. List the client sources you build in `client_sources`. They are no longer guessed from which inputs exist, so a
   config that omits the list builds the core only.
3. Client inputs go under `[client_inputs]` in lower case (`concerts_csv`, ...). The measurements source now needs
   `vault_db`, the full path of its database; `notes_vault` means only the folder of citation notes.
4. `KNOWLEDGE_PRIVATE_DIR` and the sibling `knowledge-private` convention are gone. Per-machine differences go in
   `config.local.toml` beside the config, or in environment variables (`KNOWLEDGE_DB_DIR`, ...). A dotfile manager can
   own the shared file.
5. The MCP server now runs under a mode: `mcp_mode = "off" | "normal" | "private"` (default `normal`, which hides
   private facts and subjects). Set `private` to see everything.

### Added

- Configuration in TOML, read with pydantic-settings: `~` expansion per machine, `config.local.toml` overrides,
  `KNOWLEDGE_<NAME>` environment overrides, errors that name the file.
- `knowledge.py db export | verify | import`: a deterministic text export of the database (one file per table with
  manifests and hashes) and an import into a new database; selectable by table and group; `bulk/` and `notes/` are
  git-ignored by default.
- `knowledge.py db snapshot | snapshots | restore | restore-tables`: verified online-backup snapshots with
  retention, and an in-place restore of chosen tables in one transaction after a safety snapshot.
- Audit, access and operation log tables with generated triggers and an append-only guard for fact history. Built and
  tested, **not yet installed by any command** (see `docs/db-source-of-truth.md`).
- Claude skills `knowledge-recall`, `knowledge-record` and `knowledge-freshness` (`skills/`, `docs/skills.md`);
  `recheck_by` is now in search rows.
- The stdio MCP server (`mcp_server.py`) is part of the release.
- GitHub Actions CI: tests, architecture gates and types on every pull request (ubuntu, macOS; Windows reported).
- Arborescent: every module declares its architecture ring (`__arbor__`), and a test requires a module's docstring to
  name its tach layer and describe its public functions (a shrinking allowlist covers older modules).
- Design and research docs: `docs/db-source-of-truth.md`, `docs/prior-art.md`.

### Changed

- The measurements database is its own setting (`vault_db`); `Locations.private_dir` is now `config_file`.
- mypy covers the new modules (107 files).

### Fixed

- `add-fact` no longer crashes after writing a fact when the data file is on a different drive than the code
  (Windows).
- The golden pipeline test no longer pins a hash that depends on the SQLite version.
- Exports read every table from one consistent moment; restore-tables no longer lets refused attempts push out retained
  snapshots; table and note names with a trailing newline are refused; keyword-named columns and paths with `?`, `#`
  or `%` work; database and OS errors in the `db` commands are reported as `error: ...`.
