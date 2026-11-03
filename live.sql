-- The live-database fragment (docs/db-source-of-truth.md, "Logging"): tables only the live, written database has.
-- It is NOT part of schema.sql: the rebuild (build.py) neither creates nor needs these tables, so the pipeline and its
-- schema fingerprint are untouched. `live_schema_store.install` applies this file, then generates the audit triggers
-- from `audit_rules`. Every statement is idempotent (IF NOT EXISTS).
--
-- Everything here is private: none of these tables is in normal_rules.TABLES, and tests fail if one ever is.

-- One row per insert/update/delete on a curated table (written by triggers). old_json / new_json hold the whole row.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at  TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    row_key     TEXT NOT NULL,                 -- JSON array of the primary-key values
    op          TEXT NOT NULL CHECK (op IN ('insert','update','delete')),
    old_json    TEXT,
    new_json    TEXT,
    via         TEXT NOT NULL,                 -- cli, mcp, inbox, import, restore, ... or 'unknown' (never skipped)
    session_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_row ON audit_log(table_name, row_key);

-- Who is writing right now: the write use case sets this row on its connection before it writes (single writer, so one
-- row is enough). `at` overrides the SQL clock so the application's Clock decides the time.
CREATE TABLE IF NOT EXISTS audit_context (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    via         TEXT NOT NULL,
    session_id  TEXT,
    at          TEXT
);

-- Each time a fact is read through a driving adapter. Keyed by source_key, never by statement text.
CREATE TABLE IF NOT EXISTS access_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    accessed_at  TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('show','history','search-hit','list')),
    via          TEXT NOT NULL,
    session_id   TEXT,
    mode         TEXT NOT NULL CHECK (mode IN ('off','normal','private'))
);
CREATE INDEX IF NOT EXISTS idx_access_log_key ON access_log(source_key, accessed_at);

-- What retention rolls old access rows up into, so deleting them loses no count.
CREATE TABLE IF NOT EXISTS access_summary (
    source_key        TEXT PRIMARY KEY,
    last_accessed_at  TEXT NOT NULL,
    access_count      INTEGER NOT NULL CHECK (access_count >= 0)
);

-- One row per command or run. detail_json never holds statement text, only keys and counts.
CREATE TABLE IF NOT EXISTS operation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    name         TEXT NOT NULL,
    via          TEXT NOT NULL,
    session_id   TEXT,
    outcome      TEXT NOT NULL CHECK (outcome IN ('ok','failed','refused')),
    detail_json  TEXT,
    duration_ms  INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

-- The audit and operation logs are append-only. The access log may be pruned (retention) but never edited.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS operation_log_no_update BEFORE UPDATE ON operation_log
BEGIN SELECT RAISE(ABORT, 'operation_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS operation_log_no_delete BEFORE DELETE ON operation_log
BEGIN SELECT RAISE(ABORT, 'operation_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS access_log_no_update BEFORE UPDATE ON access_log
BEGIN SELECT RAISE(ABORT, 'access_log rows are never edited'); END;
