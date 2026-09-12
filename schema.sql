-- knowledge.db canonical schema.
-- This file is the single source of truth for structure. knowledge.db itself
-- is a BUILD ARTIFACT of this schema plus the ingest_*.py / seed_*.py scripts
-- in this directory, run in order by build.py. Never hand-edit the .db file
-- or run ad hoc ALTER/INSERT against it -- change a script here and rebuild.
--
-- Normalization rule of thumb applied throughout: if a column's values repeat
-- across rows and you'd ever ask "how many/which/all" about those values as a
-- group, it's an entity with its own table (subjects, authors, metrics,
-- artists/venues/festivals) -- never a repeated text field.

PRAGMA foreign_keys = ON;

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
-- Authors: people/orgs credited on a source. Many-to-many via source_authors
-- (a source can have several authors; the same author writes several sources).
-- ============================================================
CREATE TABLE authors (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE VIRTUAL TABLE authors_fts USING fts5(name, content='authors', content_rowid='id');
CREATE TRIGGER authors_fts_ai AFTER INSERT ON authors BEGIN
  INSERT INTO authors_fts(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER authors_fts_ad AFTER DELETE ON authors BEGIN
  INSERT INTO authors_fts(authors_fts, rowid, name) VALUES ('delete', old.id, old.name);
END;
CREATE TRIGGER authors_fts_au AFTER UPDATE ON authors BEGIN
  INSERT INTO authors_fts(authors_fts, rowid, name) VALUES ('delete', old.id, old.name);
  INSERT INTO authors_fts(rowid, name) VALUES (new.id, new.name);
END;

-- ============================================================
-- Sources: anything a fact or measurement can cite.
-- ============================================================
CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    citekey         TEXT,               -- stable id, e.g. matches vault sources/<citekey>.md
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('primary','secondary','tertiary')),
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

CREATE TABLE source_authors (
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    author_id       INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    author_order    INTEGER NOT NULL DEFAULT 0,   -- position in the byline
    PRIMARY KEY (source_id, author_id)
);
CREATE INDEX idx_source_authors_author ON source_authors(author_id);

CREATE VIRTUAL TABLE sources_fts USING fts5(
  name, description, content='sources', content_rowid='id'
);
CREATE TRIGGER sources_fts_ai AFTER INSERT ON sources BEGIN
  INSERT INTO sources_fts(rowid, name, description) VALUES (new.id, new.name, new.description);
END;
CREATE TRIGGER sources_fts_ad AFTER DELETE ON sources BEGIN
  INSERT INTO sources_fts(sources_fts, rowid, name, description) VALUES ('delete', old.id, old.name, old.description);
END;
CREATE TRIGGER sources_fts_au AFTER UPDATE ON sources BEGIN
  INSERT INTO sources_fts(sources_fts, rowid, name, description) VALUES ('delete', old.id, old.name, old.description);
  INSERT INTO sources_fts(rowid, name, description) VALUES (new.id, new.name, new.description);
END;

CREATE VIEW v_sources AS
SELECT s.id, s.citekey, s.name, s.source_type,
       GROUP_CONCAT(a.name, '; ') AS authors,
       s.publisher, s.url, s.published_date, s.origin_path
FROM sources s
LEFT JOIN source_authors sa ON sa.source_id = s.id
LEFT JOIN authors a ON a.id = sa.author_id
GROUP BY s.id;

-- ============================================================
-- Metrics: the catalog of measurable things -- unit, display label, and
-- (for a handful) which direction is favorable, kept in ONE place rather
-- than re-typed on every measurement row or duplicated into UI code.
-- ============================================================
CREATE TABLE metrics (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,   -- e.g. 'body_fat_pct', 'set_bench_press_weight_lb'
    label           TEXT NOT NULL,          -- human-readable, e.g. 'Body fat'
    unit            TEXT,
    good_direction  INTEGER CHECK (good_direction IN (-1, 0, 1))  -- 1 = up is favorable, -1 = down, 0/NULL = neutral
);

-- ============================================================
-- Facts: interpretive/qualitative claims. Numeric+repeatable data belongs in
-- `measurements` instead -- see feedback_structured_vs_prose_facts memory.
-- ============================================================
CREATE TABLE facts (
    id                      INTEGER PRIMARY KEY,
    subject_id              INTEGER NOT NULL REFERENCES subjects(id),
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
CREATE INDEX idx_facts_subject ON facts(subject_id);
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

-- ============================================================
-- Measurements: structured numeric readings, one row per (metric, date).
-- ============================================================
CREATE TABLE measurements (
    id                  INTEGER PRIMARY KEY,
    subject_id          INTEGER NOT NULL REFERENCES subjects(id),
    metric_id           INTEGER NOT NULL REFERENCES metrics(id),
    value               REAL NOT NULL,
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
CREATE INDEX idx_measurements_metric ON measurements(metric_id, measured_at);
CREATE INDEX idx_measurements_subject ON measurements(subject_id);
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
-- Training sets, food log, meal log: each is one EVENT with several
-- co-occurring attributes (an exercise + weight + reps + RIR; a food +
-- its macros; a meal + its totals) -- not independent measurements that
-- happen to share a date. Exploding these into per-field `measurements`
-- rows was the same mistake the original flat concerts `events` table
-- made: it left exercise/food names with nowhere structured to live, so
-- they ended up baked into metric-key slugs or notes text instead.
-- ============================================================
CREATE TABLE exercises (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE training_sets (
    id              INTEGER PRIMARY KEY,
    exercise_id     INTEGER NOT NULL REFERENCES exercises(id),
    measured_at     TEXT NOT NULL,
    weight          REAL,
    weight_unit     TEXT,
    reps            REAL,
    rir             REAL,
    is_warmup       INTEGER NOT NULL DEFAULT 0 CHECK (is_warmup IN (0,1)),
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    notes           TEXT,
    date_added      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_training_sets_exercise ON training_sets(exercise_id);
CREATE INDEX idx_training_sets_date ON training_sets(measured_at);

CREATE TABLE foods (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE food_log_entries (
    id              INTEGER PRIMARY KEY,
    food_id         INTEGER NOT NULL REFERENCES foods(id),
    measured_at     TEXT NOT NULL,
    time            TEXT,
    serving_qty     REAL,
    serving_size    TEXT,
    calories_kcal   REAL,
    fat_g           REAL,
    carbs_g         REAL,
    protein_g       REAL,
    alcohol_g       REAL,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    date_added      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_food_log_food ON food_log_entries(food_id);
CREATE INDEX idx_food_log_date ON food_log_entries(measured_at);

CREATE TABLE meal_log_entries (
    id                  INTEGER PRIMARY KEY,
    measured_at         TEXT NOT NULL,
    meal                TEXT NOT NULL,     -- 'Breakfast'/'Lunch'/etc. -- low-cardinality, not worth its own table
    calories_kcal       REAL,
    fat_g               REAL,
    saturated_fat_g     REAL,
    carbs_g             REAL,
    fiber_g             REAL,
    sugar_g             REAL,
    protein_g           REAL,
    sodium_mg           REAL,
    potassium_mg        REAL,
    cholesterol_mg      REAL,
    vitamin_a           REAL,
    vitamin_c           REAL,
    calcium             REAL,
    iron                REAL,
    source_id           INTEGER NOT NULL REFERENCES sources(id),
    date_added          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_meal_log_date ON meal_log_entries(measured_at);

CREATE VIEW v_training_sets AS
SELECT ts.id, e.name AS exercise, ts.measured_at, ts.weight, ts.weight_unit, ts.reps, ts.rir, ts.is_warmup, s.name AS source
FROM training_sets ts JOIN exercises e ON e.id = ts.exercise_id JOIN sources s ON s.id = ts.source_id;

CREATE VIEW v_food_log AS
SELECT fl.id, f.name AS food, fl.measured_at, fl.time, fl.serving_qty, fl.serving_size,
       fl.calories_kcal, fl.fat_g, fl.carbs_g, fl.protein_g, fl.alcohol_g
FROM food_log_entries fl JOIN foods f ON f.id = fl.food_id;

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
    f.id AS fact_id, sub.name AS subject, f.statement, f.is_original_claim,
    f.trust_level, f.trust_rationale, f.date_added, f.recheck_by, f.recheck_rationale,
    GROUP_CONCAT(s.name, ' | ') AS sources,
    GROUP_CONCAT(s.source_type, ' | ') AS source_types
FROM facts f
JOIN subjects sub ON sub.id = f.subject_id
LEFT JOIN fact_sources fs ON fs.fact_id = f.id
LEFT JOIN sources s ON s.id = fs.source_id
GROUP BY f.id;

CREATE VIEW measurement_with_source AS
SELECT
    m.id AS measurement_id, sub.name AS subject, met.key AS metric, met.label AS metric_label,
    m.value, met.unit, m.measured_at,
    m.trust_level, s.name AS source_name, s.source_type
FROM measurements m
JOIN subjects sub ON sub.id = m.subject_id
JOIN metrics met ON met.id = m.metric_id
JOIN sources s ON s.id = m.source_id;

CREATE VIEW v_fact_tags AS
SELECT
    f.id AS fact_id, f.statement, f.trust_level, f.is_original_claim, f.is_personal, f.status,
    s.name AS subject, s.domain, p.name AS parent_subject
FROM facts f
JOIN subjects s ON s.id = f.subject_id
LEFT JOIN subjects p ON p.id = s.parent_id;

-- ============================================================
-- Full-text search over facts (requires an FTS5-enabled sqlite3 -- Python's
-- built-in module has it; this machine's `sqlite3` CLI does not).
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
