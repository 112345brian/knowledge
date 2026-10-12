"""SQLite adapter for `normal_db_port.NormalSource` and `NormalSink`: it reads the full DB's rows and writes the
normal DB. It copies into a FRESH, empty database (never deletes from a copy of the full DB: deletion leaves old
pages, free-list garbage and FTS shadow rows holding the deleted text). It decides nothing; which rows go in is
`normal_rules.select_content` (the visibility filter on facts and revisions below is only the read's narrowing: the
rules check each row's visibility again, and a private entity is read with its flag and dropped there). Stateless; the module is the adapter."""
import os
import sqlite3

from normal_rules import (FACT_COLS, FTS_TABLES, REVISION_COLS, SOURCE_COLS, TABLES, FullDbRows, Integrity)

SCHEMA = """
CREATE TABLE build_info (
    id              INTEGER PRIMARY KEY,
    built_at        TEXT NOT NULL,
    schema_version  INTEGER NOT NULL
);
CREATE TABLE source_identifiers (
    source_id  INTEGER NOT NULL REFERENCES sources(id),
    scheme     TEXT NOT NULL CHECK (scheme IN ('doi','isbn','issn','pmid','arxiv','other')),
    value      TEXT NOT NULL CHECK (length(trim(value)) > 0),
    PRIMARY KEY (source_id, scheme, value),
    UNIQUE (scheme, value)
);
CREATE TABLE subjects (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    domain     TEXT NOT NULL,
    parent_id  INTEGER REFERENCES subjects(id),
    description            TEXT,
    parent_relation        TEXT NOT NULL DEFAULT 'broader' CHECK (parent_relation IN ('broader','part-of','subtype-of','member-of')),
    deprecated             INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),
    replaced_by_subject_id INTEGER REFERENCES subjects(id),
    CHECK (replaced_by_subject_id IS NULL OR deprecated = 1)
);
CREATE TABLE subject_aliases (
    subject_id  INTEGER NOT NULL REFERENCES subjects(id),
    alias       TEXT NOT NULL UNIQUE,
    PRIMARY KEY (subject_id, alias)
);
CREATE TABLE entities (
    id              INTEGER PRIMARY KEY,
    entity_key      TEXT NOT NULL UNIQUE,
    canonical_name  TEXT NOT NULL,
    name_norm       TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL CHECK (type IN ('person','organization','place','project','substance','other')),
    private         INTEGER NOT NULL DEFAULT 0 CHECK (private = 0),
    external_id     TEXT,
    notes           TEXT
);
CREATE TABLE entity_aliases (
    entity_id   INTEGER NOT NULL REFERENCES entities(id),
    alias       TEXT NOT NULL,
    alias_norm  TEXT NOT NULL UNIQUE,
    PRIMARY KEY (entity_id, alias)
);
CREATE TABLE fact_entities (
    fact_id    INTEGER NOT NULL REFERENCES facts(id),
    entity_id  INTEGER NOT NULL REFERENCES entities(id),
    PRIMARY KEY (fact_id, entity_id)
);
CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    citekey         TEXT,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('primary','secondary','tertiary')),
    publisher_id    INTEGER REFERENCES publishers(id),
    url             TEXT,
    published_date  TEXT,
    retrieved_date  TEXT,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','corrected','expression-of-concern','retracted','superseded')),
    edition         TEXT
);
CREATE UNIQUE INDEX idx_sources_citekey ON sources(citekey);
CREATE TABLE source_authors (
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    author_id     INTEGER NOT NULL REFERENCES authors(id),
    author_order  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, author_id)
);
CREATE TABLE facts (
    id                     INTEGER PRIMARY KEY,
    subject_id             INTEGER NOT NULL REFERENCES subjects(id),
    statement              TEXT NOT NULL,
    is_original_claim      INTEGER NOT NULL CHECK (is_original_claim IN (0,1)),
    trust_level            TEXT NOT NULL CHECK (trust_level IN ('verified','high','medium','low','unverified','disputed')),
    trust_rationale        TEXT,
    status                 TEXT NOT NULL CHECK (status IN ('pending','active','superseded','retracted')),
    superseded_by_fact_id  INTEGER REFERENCES facts(id),
    date_added             TEXT NOT NULL,
    last_reviewed_at       TEXT,
    recheck_by             TEXT,
    recheck_rationale      TEXT,
    visibility             TEXT NOT NULL CHECK (visibility = 'normal'),
    source_key             TEXT UNIQUE,
    freshness              TEXT NOT NULL CHECK (freshness IN ('recheck','no-decay','unreviewed')),
    kind                   TEXT NOT NULL CHECK (kind IN ('observation','measurement','decision','preference','plan','definition','inference','rule','lesson','unclassified')),
    applies_to             TEXT CHECK (applies_to IS NULL OR length(trim(applies_to, char(32, 9, 10, 11, 12, 13))) > 0),
    valid_from             TEXT CHECK (valid_from IS NULL OR valid_from GLOB '[0-9][0-9][0-9][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    valid_to               TEXT CHECK (valid_to IS NULL OR valid_to GLOB '[0-9][0-9][0-9][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR substr(valid_from, 1, min(length(valid_from), length(valid_to))) <= substr(valid_to, 1, min(length(valid_from), length(valid_to)))),
    CHECK (freshness <> 'recheck' OR recheck_by IS NOT NULL),
    CHECK (freshness <> 'no-decay' OR COALESCE(length(trim(recheck_rationale, char(32, 9, 10, 11, 12, 13))), 0) > 0)
);
CREATE INDEX idx_facts_subject ON facts(subject_id);
CREATE TABLE fact_revisions (
    id                 INTEGER PRIMARY KEY,
    fact_id            INTEGER NOT NULL REFERENCES facts(id),
    source_key         TEXT NOT NULL,
    revision           INTEGER NOT NULL CHECK (revision >= 1),
    changed_at         TEXT NOT NULL,
    changed_via        TEXT NOT NULL,
    statement          TEXT NOT NULL,
    trust_level        TEXT NOT NULL CHECK (trust_level IN ('verified','high','medium','low','unverified','disputed')),
    trust_rationale    TEXT,
    status             TEXT NOT NULL CHECK (status IN ('pending','active','superseded','retracted')),
    visibility         TEXT NOT NULL CHECK (visibility = 'normal'),
    superseded_by      TEXT,
    recheck_by         TEXT,
    recheck_rationale  TEXT,
    freshness          TEXT,
    kind               TEXT NOT NULL CHECK (kind IN ('observation','measurement','decision','preference','plan','definition','inference','rule','lesson','unclassified')),
    applies_to         TEXT CHECK (applies_to IS NULL OR length(trim(applies_to, char(32, 9, 10, 11, 12, 13))) > 0),
    valid_from         TEXT CHECK (valid_from IS NULL OR valid_from GLOB '[0-9][0-9][0-9][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    valid_to           TEXT CHECK (valid_to IS NULL OR valid_to GLOB '[0-9][0-9][0-9][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR substr(valid_from, 1, min(length(valid_from), length(valid_to))) <= substr(valid_to, 1, min(length(valid_from), length(valid_to)))),
    UNIQUE (source_key, revision)
);
CREATE INDEX idx_fact_revisions_fact ON fact_revisions(fact_id, revision);
CREATE TABLE fact_sources (
    fact_id    INTEGER NOT NULL REFERENCES facts(id),
    source_id  INTEGER NOT NULL REFERENCES sources(id),
    locator    TEXT,
    quote      TEXT,
    PRIMARY KEY (fact_id, source_id)
);
CREATE INDEX idx_fact_sources_source ON fact_sources(source_id);
CREATE VIRTUAL TABLE facts_fts USING fts5(statement, trust_rationale, applies_to, content='facts', content_rowid='id');
CREATE VIRTUAL TABLE sources_fts USING fts5(name, description, content='sources', content_rowid='id');
CREATE VIRTUAL TABLE authors_fts USING fts5(name, content='authors', content_rowid='id');
CREATE VIEW v_subjects AS
SELECT s.id, s.name, s.domain, s.parent_id, COUNT(f.id) AS fact_count
FROM subjects s JOIN facts f ON f.subject_id = s.id
GROUP BY s.id;
"""


def _open_readonly(path):
    return sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)


def read_rows(full_path):
    full = _open_readonly(full_path)
    try:
        def q(sql):
            return full.execute(sql).fetchall()

        latest = full.execute("SELECT id, built_at, schema_version FROM build_info ORDER BY id DESC LIMIT 1").fetchone()
        return FullDbRows(
            subjects=q("SELECT id, name, parent_id, private, domain, description, parent_relation, deprecated, "
                       "replaced_by_subject_id FROM subjects ORDER BY id"),
            subject_aliases=q("SELECT subject_id, alias FROM subject_aliases ORDER BY subject_id, alias"),
            citations=q("SELECT fact_id, source_id, locator, quote FROM fact_sources ORDER BY fact_id, source_id"),
            facts=q(f"SELECT {FACT_COLS} FROM facts WHERE visibility = 'normal' ORDER BY id"),
            revisions=q(f"SELECT {REVISION_COLS} FROM fact_revisions WHERE visibility = 'normal' ORDER BY id"),
            sources=q(f"SELECT {SOURCE_COLS} FROM sources ORDER BY id"),
            publishers=q("SELECT id, name FROM publishers ORDER BY id"),
            authors=q("SELECT id, name FROM authors ORDER BY id"),
            source_authors=q("SELECT source_id, author_id, author_order FROM source_authors ORDER BY source_id, author_id"),
            identifiers=q("SELECT source_id, scheme, value FROM source_identifiers ORDER BY source_id, scheme, value"),
            entities=q("SELECT id, entity_key, canonical_name, name_norm, type, private, external_id, notes "
                       "FROM entities ORDER BY id"),
            entity_aliases=q("SELECT entity_id, alias, alias_norm FROM entity_aliases ORDER BY entity_id, alias"),
            fact_entities=q("SELECT fact_id, entity_id FROM fact_entities ORDER BY fact_id, entity_id"),
            build_info=tuple(latest) if latest else None,
        )
    finally:
        full.close()


def write(path, content):
    out = sqlite3.connect(path)
    try:
        out.execute("PRAGMA journal_mode = DELETE")
        out.executescript(SCHEMA)
        for part in content:
            if not part.rows:
                continue
            marks = ",".join("?" * len(part.columns))
            out.executemany(f"INSERT INTO {part.table} ({', '.join(part.columns)}) VALUES ({marks})", part.rows)
        for fts in FTS_TABLES:
            out.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")
        out.commit()
    finally:
        out.close()


def inspect(path):
    con = sqlite3.connect(path)
    try:
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        triggers = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0]
        return Integrity(bad, counts, names, triggers)
    finally:
        con.close()
