-- knowledge.db canonical schema.
-- This file is the single source of truth for structure. knowledge.db itself
-- is a BUILD ARTIFACT of this schema plus the ingest_*.py / seed_*.py scripts
-- in this directory, run in order by build.py. Never hand-edit the .db file
-- or run ad hoc ALTER/INSERT against it -- change a script here and rebuild.

PRAGMA foreign_keys = ON;

-- ============================================================
-- Sources: anything a fact or measurement can cite.
-- ============================================================
CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    citekey         TEXT,               -- stable id, e.g. matches vault sources/<citekey>.md
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('primary','secondary','tertiary')),
    author          TEXT,
    publisher       TEXT,
    url             TEXT,
    published_date  TEXT,
    retrieved_date  TEXT,
    description     TEXT,
    origin_path     TEXT,               -- the actual file/table this source came from
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_sources_citekey ON sources(citekey);
CREATE INDEX idx_sources_origin_path ON sources(origin_path);

-- ============================================================
-- Subjects: topic tags, arranged in a shallow tree (domain -> parent -> subject).
-- ============================================================
CREATE TABLE subjects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    domain      TEXT NOT NULL DEFAULT 'health-and-fitness',
    parent_id   INTEGER REFERENCES subjects(id)
);

-- ============================================================
-- Facts: interpretive/qualitative claims. Numeric+repeatable data belongs in
-- `measurements` instead -- see feedback_structured_vs_prose_facts memory.
-- ============================================================
CREATE TABLE facts (
    id                      INTEGER PRIMARY KEY,
    subject                 TEXT NOT NULL,
    statement               TEXT NOT NULL,
    is_original_claim       INTEGER NOT NULL DEFAULT 0 CHECK (is_original_claim IN (0,1)),
    is_personal             INTEGER NOT NULL DEFAULT 1 CHECK (is_personal IN (0,1)),
    trust_level             TEXT NOT NULL CHECK (trust_level IN ('verified','high','medium','low','unverified','disputed')),
    trust_rationale         TEXT,
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted')),
    superseded_by_fact_id   INTEGER REFERENCES facts(id),
    provided_by             TEXT NOT NULL DEFAULT 'user',
    date_added              TEXT NOT NULL DEFAULT (datetime('now')),
    last_reviewed_at        TEXT,
    recheck_by              TEXT,
    recheck_rationale       TEXT,
    origin_path             TEXT,       -- the vault file this fact was extracted from
    notes                   TEXT
);
CREATE INDEX idx_facts_subject ON facts(subject);
CREATE INDEX idx_facts_trust ON facts(trust_level);
CREATE INDEX idx_facts_is_personal ON facts(is_personal);
CREATE INDEX idx_facts_status ON facts(status);
CREATE INDEX idx_facts_origin_path ON facts(origin_path);

CREATE TABLE fact_sources (
    fact_id     INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    locator     TEXT,
    quote       TEXT,
    PRIMARY KEY (fact_id, source_id)
);
CREATE INDEX idx_fact_sources_fact ON fact_sources(fact_id);
CREATE INDEX idx_fact_sources_source ON fact_sources(source_id);

CREATE TABLE fact_subjects (
    fact_id     INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, subject_id)
);

-- ============================================================
-- Measurements: structured numeric readings, one row per (metric, date).
-- ============================================================
CREATE TABLE measurements (
    id                  INTEGER PRIMARY KEY,
    subject             TEXT NOT NULL,
    metric              TEXT NOT NULL,      -- e.g. 'body_fat_pct', 'bmd_zscore'
    value               REAL NOT NULL,
    unit                TEXT,
    measured_at         TEXT NOT NULL,      -- date the measurement was actually taken
    source_id           INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    trust_level         TEXT NOT NULL CHECK (trust_level IN ('verified','high','medium','low','unverified','disputed')),
    trust_rationale      TEXT,
    is_personal         INTEGER NOT NULL DEFAULT 1 CHECK (is_personal IN (0,1)),
    date_added          TEXT NOT NULL DEFAULT (datetime('now')),
    last_reviewed_at    TEXT,
    recheck_by          TEXT,
    recheck_rationale   TEXT,
    notes               TEXT
);
CREATE INDEX idx_measurements_metric ON measurements(metric, measured_at);
CREATE INDEX idx_measurements_subject ON measurements(subject);
CREATE INDEX idx_measurements_is_personal ON measurements(is_personal);

CREATE TABLE fact_measurements (
    fact_id         INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    measurement_id  INTEGER NOT NULL REFERENCES measurements(id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, measurement_id)
);

-- ============================================================
-- Claims: broader conclusions, citing the specific facts backing them.
-- ============================================================
CREATE TABLE claims (
    id          INTEGER PRIMARY KEY,
    statement   TEXT NOT NULL,
    date_added  TEXT NOT NULL DEFAULT (datetime('now')),
    notes       TEXT
);

CREATE TABLE claim_facts (
    claim_id    INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    fact_id     INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    PRIMARY KEY (claim_id, fact_id)
);

-- ============================================================
-- Concert-going, normalized: artists/venues/festivals as their own entities
-- (so "how many times have I seen X" or "every act at festival Y" is a
-- join, not a text match), with `concert_attendances` as the fact table
-- linking them to a specific date. Not a claim needing trust/provenance,
-- not a numeric metric -- a third shape, not a reuse of facts/measurements.
-- ============================================================
CREATE TABLE artists (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE venues (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    city_state  TEXT,
    UNIQUE (name, city_state)
);

CREATE TABLE festivals (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE concert_attendances (
    id                      INTEGER PRIMARY KEY,
    artist_id               INTEGER NOT NULL REFERENCES artists(id),
    start_date              TEXT NOT NULL,
    end_date                TEXT,
    venue_id                INTEGER REFERENCES venues(id),
    festival_id             INTEGER REFERENCES festivals(id),
    billing                 TEXT NOT NULL DEFAULT 'headliner' CHECK (billing IN ('headliner','opener','festival-set')),
    supporting_for_artist_id INTEGER REFERENCES artists(id),   -- set when billing = 'opener'
    notes                   TEXT,
    domain                  TEXT NOT NULL DEFAULT 'music',
    source_file             TEXT,
    date_added              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_concert_attendances_artist ON concert_attendances(artist_id);
CREATE INDEX idx_concert_attendances_venue ON concert_attendances(venue_id);
CREATE INDEX idx_concert_attendances_festival ON concert_attendances(festival_id);
CREATE INDEX idx_concert_attendances_date ON concert_attendances(start_date);
CREATE INDEX idx_concert_attendances_billing ON concert_attendances(billing);

CREATE VIEW v_concert_attendances AS
SELECT
    ca.id, a.name AS artist, ca.start_date, ca.end_date,
    v.name AS venue, v.city_state, f.name AS festival,
    ca.billing, sa.name AS supporting_for, ca.notes
FROM concert_attendances ca
JOIN artists a ON a.id = ca.artist_id
LEFT JOIN venues v ON v.id = ca.venue_id
LEFT JOIN festivals f ON f.id = ca.festival_id
LEFT JOIN artists sa ON sa.id = ca.supporting_for_artist_id;

-- ============================================================
-- Convenience views
-- ============================================================
CREATE VIEW fact_with_sources AS
SELECT
    f.id AS fact_id, f.subject, f.statement, f.is_original_claim,
    f.trust_level, f.trust_rationale, f.date_added, f.recheck_by, f.recheck_rationale,
    GROUP_CONCAT(s.name, ' | ') AS sources,
    GROUP_CONCAT(s.source_type, ' | ') AS source_types
FROM facts f
LEFT JOIN fact_sources fs ON fs.fact_id = f.id
LEFT JOIN sources s ON s.id = fs.source_id
GROUP BY f.id;

CREATE VIEW measurement_with_source AS
SELECT
    m.id AS measurement_id, m.subject, m.metric, m.value, m.unit, m.measured_at,
    m.trust_level, s.name AS source_name, s.source_type
FROM measurements m
JOIN sources s ON s.id = m.source_id;

CREATE VIEW v_fact_tags AS
SELECT
    f.id AS fact_id, f.statement, f.trust_level, f.is_original_claim, f.is_personal, f.status,
    s.name AS subject, s.domain, p.name AS parent_subject
FROM facts f
JOIN subjects s ON s.name = f.subject
LEFT JOIN subjects p ON p.id = s.parent_id;

-- ============================================================
-- Full-text search (requires an FTS5-enabled sqlite3 -- Python's built-in
-- module has it; this machine's `sqlite3` CLI does not).
-- ============================================================
CREATE VIRTUAL TABLE facts_fts USING fts5(
  statement, trust_rationale, notes, content='facts', content_rowid='id'
);
CREATE TRIGGER facts_fts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, statement, trust_rationale, notes) VALUES (new.id, new.statement, new.trust_rationale, new.notes);
END;
CREATE TRIGGER facts_fts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, statement, trust_rationale, notes) VALUES ('delete', old.id, old.statement, old.trust_rationale, old.notes);
END;
CREATE TRIGGER facts_fts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, statement, trust_rationale, notes) VALUES ('delete', old.id, old.statement, old.trust_rationale, old.notes);
  INSERT INTO facts_fts(rowid, statement, trust_rationale, notes) VALUES (new.id, new.statement, new.trust_rationale, new.notes);
END;

CREATE VIRTUAL TABLE sources_fts USING fts5(
  name, author, description, content='sources', content_rowid='id'
);
CREATE TRIGGER sources_fts_ai AFTER INSERT ON sources BEGIN
  INSERT INTO sources_fts(rowid, name, author, description) VALUES (new.id, new.name, new.author, new.description);
END;
CREATE TRIGGER sources_fts_ad AFTER DELETE ON sources BEGIN
  INSERT INTO sources_fts(sources_fts, rowid, name, author, description) VALUES ('delete', old.id, old.name, old.author, old.description);
END;
CREATE TRIGGER sources_fts_au AFTER UPDATE ON sources BEGIN
  INSERT INTO sources_fts(sources_fts, rowid, name, author, description) VALUES ('delete', old.id, old.name, old.author, old.description);
  INSERT INTO sources_fts(rowid, name, author, description) VALUES (new.id, new.name, new.author, new.description);
END;
