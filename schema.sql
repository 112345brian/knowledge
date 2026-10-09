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
    parent_id   INTEGER REFERENCES subjects(id),
    -- #31 privacy: 1 when this subject or an ancestor is tagged private in privacy_rules.json
    -- (set by privacy.apply_rules_to_db at the end of 04/11; the rules file is the source of truth).
    private     INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0,1))
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
-- Publishers: journals/publishers repeat across sources (JCEM, Sports
-- Medicine, Human Kinetics, ...) -- worth a table for "everything from
-- this journal", even though most sources have a unique one.
-- ============================================================
CREATE TABLE publishers (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

-- ============================================================
-- Sources: anything a fact or measurement can cite.
-- ============================================================
CREATE TABLE sources (
    id              INTEGER PRIMARY KEY,
    citekey         TEXT,               -- stable id, e.g. matches vault sources/<citekey>.md
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('primary','secondary','tertiary')),
    publisher_id    INTEGER REFERENCES publishers(id),
    url             TEXT,
    published_date  TEXT,
    retrieved_date  TEXT,
    description     TEXT,
    origin_path     TEXT,               -- the actual file/table this source came from -- NOT normalized:
                                         -- 450/451 distinct, essentially 1:1 with sources, no repetition to fix
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    -- File fixity (#38): the file as it was when the build read it. NULL hash/size/mtime with
    -- file_state = 'missing' when the file was absent or unreadable; never an invented hash.
    content_sha256  TEXT CHECK (content_sha256 IS NULL OR length(content_sha256) = 64),
    size_bytes      INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    file_mtime      TEXT,
    mime_type       TEXT,
    file_state      TEXT CHECK (file_state IS NULL OR file_state IN ('present','missing'))
);
CREATE UNIQUE INDEX idx_sources_citekey ON sources(citekey);
CREATE INDEX idx_sources_origin_path ON sources(origin_path);
CREATE INDEX idx_sources_publisher ON sources(publisher_id);

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
       p.name AS publisher, s.url, s.published_date, s.origin_path
FROM sources s
LEFT JOIN source_authors sa ON sa.source_id = s.id
LEFT JOIN authors a ON a.id = sa.author_id
LEFT JOIN publishers p ON p.id = s.publisher_id
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
-- Vault files: 61 distinct files back 295 facts (~5 facts/file) -- real
-- repetition, and a place to hang file-level metadata (last synced, etc.)
-- instead of it being implicit across every fact row from that file.
-- ============================================================
CREATE TABLE vault_files (
    id      INTEGER PRIMARY KEY,
    path    TEXT NOT NULL UNIQUE,
    -- File fixity (#38): the file as it was when the build read it. NULL hash/size/mtime with
    -- file_state = 'missing' when the file was absent or unreadable; never an invented hash.
    content_sha256  TEXT CHECK (content_sha256 IS NULL OR length(content_sha256) = 64),
    size_bytes      INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    file_mtime      TEXT,
    mime_type       TEXT,
    file_state      TEXT CHECK (file_state IS NULL OR file_state IN ('present','missing'))
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
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('pending','active','superseded','retracted')),  -- 'pending' added by #30 (revisions can set it; #6 owns the workflow)
    superseded_by_fact_id   INTEGER REFERENCES facts(id),
    provided_by             TEXT NOT NULL DEFAULT 'user',
    date_added              TEXT NOT NULL DEFAULT (datetime('now')),
    last_reviewed_at        TEXT,
    recheck_by              TEXT,
    recheck_rationale       TEXT,
    origin_file_id          INTEGER REFERENCES vault_files(id),
    notes                   TEXT,
    -- Who may see this fact. NOT derived from is_personal (a keyword heuristic, not a privacy
    -- boundary). Anything unmarked is private; the value is only ever stored here, never inferred.
    visibility              TEXT NOT NULL DEFAULT 'private' CHECK (visibility IN ('private','normal')),
    -- Provenance, all optional here; add_fact.py requires session_id + source_quote when
    -- captured_via = 'mcp'. captured_via is an open vocabulary (cli, mcp, migrate-memory, ...).
    -- source_quote is the words that justified the fact; when a fact also cites a source the
    -- same text is in fact_sources.quote, which stays the per-source copy.
    captured_via            TEXT,
    session_id              TEXT,
    captured_at             TEXT,
    source_quote            TEXT,
    -- Stable identity across rebuilds (#30); fact_revisions and fact_revisions.jsonl point at it.
    -- Nullable only so hand-built test rows work; the ingest scripts always set it.
    source_key              TEXT UNIQUE,
    -- Fixity baseline (#38): the SHA-256 the origin file had when this fact was extracted (see
    -- fixity.audit_sources). NULL = no baseline recorded. Not part of the revision snapshot.
    -- Kind (#39): what sort of assertion this is. The CHECK enforces the vocabulary only; which kind a
    -- fact gets is self-reported guidance, not something the database can verify. 'unclassified' is the
    -- honest marker for every legacy fact and the default for a new one. Mutable via a revision (#30).
    kind                    TEXT NOT NULL DEFAULT 'unclassified' CHECK (kind IN ('observation','measurement','decision','preference','plan','definition','inference','rule','lesson','unclassified')),
    -- Valid time (#40): when the fact was TRUE, not when it was recorded (date_added / revisions are
    -- transaction time). ISO dates YYYY, YYYY-MM or YYYY-MM-DD; a partial date names the whole span
    -- (valid_from '2024' starts 2024-01-01, valid_to '2024-03' ends 2024-03-31). NULL valid_from: no known
    -- start; NULL valid_to: still true as far as known; a point in time sets both equal. The CHECKs
    -- enforce the shape and valid_to >= valid_from at the shorter precision; whether the date is REAL
    -- (no Feb 30) is checked by validtime.py in the writers and the ingest. A past valid_to does not
    -- change status: validity is not retraction.
    valid_from              TEXT CHECK (valid_from IS NULL OR valid_from GLOB '[0-9][0-9][0-9][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    valid_to                TEXT CHECK (valid_to IS NULL OR valid_to GLOB '[0-9][0-9][0-9][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    extracted_from_sha256   TEXT CHECK (extracted_from_sha256 IS NULL OR length(extracted_from_sha256) = 64),
    -- Freshness (#7): every fact either has a recheck_by or explicitly asserts it does not decay.
    --   recheck     the fact may go stale; recheck_by is required (first CHECK below).
    --   no-decay    an explicit assertion that the fact does not decay (a birthdate, a completed
    --               purchase); a written recheck_rationale (non-blank) is required, recheck_by may
    --               be NULL. It is NOT an excuse to default trust_level to 'verified': the fact
    --               still needs an honestly considered trust level reflecting how it was actually
    --               captured (cross-checked against an ID vs. typed from memory).
    --   unreviewed  LEGACY ONLY: predates this column and was never individually reviewed (183 of
    --               the 295 legacy facts have no recheck_by). Exempt from both rules. The add_fact /
    --               facts_batch / migrate paths REJECT it for a new fact and revisions may only
    --               move a fact OUT of it; only 04_ingest_facts.py assigns it, to entries in the
    --               legacy files that have no recheck_by.
    -- What the schema ENFORCES: only that the required field is present (recheck_by, or a
    -- non-blank rationale). It cannot judge whether a fact really does not decay, or whether a
    -- recheck_by is sensible; that stays with whoever writes the fact. Staleness only: a fact that
    -- was wrong when typed is trust_level's job.
    freshness               TEXT NOT NULL CHECK (freshness IN ('recheck','no-decay','unreviewed')),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR substr(valid_from, 1, min(length(valid_from), length(valid_to))) <= substr(valid_to, 1, min(length(valid_from), length(valid_to)))),
    CHECK (freshness <> 'recheck' OR recheck_by IS NOT NULL),
    CHECK (freshness <> 'no-decay' OR COALESCE(length(trim(recheck_rationale, char(32, 9, 10, 11, 12, 13))), 0) > 0)
);
CREATE INDEX idx_facts_subject ON facts(subject_id);
CREATE INDEX idx_facts_trust ON facts(trust_level);
CREATE INDEX idx_facts_is_personal ON facts(is_personal);
CREATE INDEX idx_facts_status ON facts(status);
CREATE INDEX idx_facts_origin_file ON facts(origin_file_id);
CREATE INDEX idx_facts_visibility ON facts(visibility);

-- ===== BEGIN #30 fact revisions (own block; keep merges separate) =====
-- One row per revision of a fact, including the implicit revision 1 (the original JSON entry,
-- written by 04_ingest_facts.py / 11_seed_general_facts.py). Revisions >= 2 come from
-- fact_revisions.jsonl via 12_apply_fact_revisions.py. `facts` holds the CURRENT state, copied
-- from each fact's latest revision; this table is the history. superseded_by is a source_key.
CREATE TABLE fact_revisions (
    id               INTEGER PRIMARY KEY,
    fact_id          INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    source_key       TEXT NOT NULL,
    revision         INTEGER NOT NULL CHECK (revision >= 1),
    changed_at       TEXT NOT NULL,
    changed_via      TEXT NOT NULL,
    session_id       TEXT,
    change_reason    TEXT,
    statement        TEXT NOT NULL,
    trust_level      TEXT NOT NULL CHECK (trust_level IN ('verified','high','medium','low','unverified','disputed')),
    trust_rationale  TEXT,
    status           TEXT NOT NULL CHECK (status IN ('pending','active','superseded','retracted')),
    visibility       TEXT NOT NULL CHECK (visibility IN ('private','normal')),
    superseded_by    TEXT,
    recheck_by       TEXT,
    recheck_rationale TEXT,
    freshness        TEXT,
    kind             TEXT NOT NULL DEFAULT 'unclassified' CHECK (kind IN ('observation','measurement','decision','preference','plan','definition','inference','rule','lesson','unclassified')),
    valid_from       TEXT CHECK (valid_from IS NULL OR valid_from GLOB '[0-9][0-9][0-9][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_from GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    valid_to         TEXT CHECK (valid_to IS NULL OR valid_to GLOB '[0-9][0-9][0-9][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    notes            TEXT,
    CHECK (valid_from IS NULL OR valid_to IS NULL OR substr(valid_from, 1, min(length(valid_from), length(valid_to))) <= substr(valid_to, 1, min(length(valid_from), length(valid_to)))),
    UNIQUE (source_key, revision)
);
CREATE INDEX idx_fact_revisions_fact ON fact_revisions(fact_id, revision);
-- ===== END #30 fact revisions =====

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
    notes       TEXT,
    -- How the cited facts (claim_facts) support the statement. Nullable and forward-only: set it on
    -- new claims; the claims that existed before this column are deliberately NOT backfilled, and
    -- NULL means "not classified", never a default type.
    inference_type TEXT CHECK (inference_type IN ('deductive','inductive','abductive'))
);

CREATE TABLE claim_facts (
    claim_id    INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    fact_id     INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    PRIMARY KEY (claim_id, fact_id)
);

-- One row per (claim, cited fact) where the fact is superseded or retracted. Claims have no status of
-- their own, so every claim counts as active. The "past recheck_by" case is NOT here: recheck_by is
-- free text that is only sometimes a date, so claims_audit.audit_claims() does that part in Python.
CREATE VIEW v_claims_with_stale_premises AS
SELECT
    c.id AS claim_id, c.statement AS claim_statement, c.inference_type,
    f.id AS fact_id, f.statement AS fact_statement, f.status AS reason,
    f.superseded_by_fact_id, f.trust_rationale, f.notes AS fact_notes
FROM claims c
JOIN claim_facts cf ON cf.claim_id = c.id
JOIN facts f ON f.id = cf.fact_id
WHERE f.status IN ('superseded','retracted');

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
-- Muscle training volume: weekly sets per muscle group. The muscle name
-- was previously baked into a metric key (chest_sets_per_week) -- same
-- flaw as exercises/foods. Sourced from the vault's own precomputed
-- muscle_volume_weekly rollup (its exercise-to-muscle-group classification
-- logic isn't reproduced here -- see 03_ingest_measurements.py comments).
-- ============================================================
CREATE TABLE muscles (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE
);

CREATE TABLE muscle_volume_weekly (
    id              INTEGER PRIMARY KEY,
    muscle_id       INTEGER NOT NULL REFERENCES muscles(id),
    week_start      TEXT NOT NULL,
    sets            REAL NOT NULL,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    date_added      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_muscle_volume_muscle ON muscle_volume_weekly(muscle_id);
CREATE INDEX idx_muscle_volume_week ON muscle_volume_weekly(week_start);

CREATE VIEW v_muscle_volume_weekly AS
SELECT mv.id, m.name AS muscle, mv.week_start, mv.sets
FROM muscle_volume_weekly mv JOIN muscles m ON m.id = mv.muscle_id;

-- ============================================================
-- Import sources: which ingest-script source file a row came from, shared
-- by every table below that would otherwise repeat that path as text on
-- every row -- the exact `sources.origin_path` situation this file's header
-- comment already calls out, except those tables don't have 450 distinct
-- values, they have ONE value repeated hundreds or (for scrobbles)
-- hundreds of thousands of times. Deliberately separate from `sources`
-- (bibliographic citations for facts/measurements) and `vault_files`
-- (specifically markdown vault notes) -- this is neither, just "the file
-- build.py's ingest script read this row from".
-- ============================================================
CREATE TABLE import_sources (
    id      INTEGER PRIMARY KEY,
    path    TEXT NOT NULL UNIQUE
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
-- Ingest scripts resolve artist identity case-insensitively in Python (see
-- get_or_create_artist in _shared.py) before ever inserting -- this index is
-- the backstop that turns a logic bug into a build failure instead of a
-- silent duplicate ("JPEGMAFIA" row 12 vs "Jpegmafia" row 340).
CREATE UNIQUE INDEX idx_artists_name_nocase ON artists(lower(name));

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
    import_source_id        INTEGER REFERENCES import_sources(id),
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
-- Artist members: a group/collab credit ("Freddie Gibbs & Madlib",
-- "Madvillain") stays ONE artist row -- same as the source data actually
-- writes it -- rather than being decomposed per track/album. Attempting
-- that decomposition (tried and reverted) ran straight into real-world
-- mess: RYM stores the ampersand HTML-escaped ("Gibbs &amp; Madlib"),
-- Last.fm scrobbles four-way feature lists as one string ("Freddie Gibbs,
-- Madlib, Domo Genesis, Earl Sweatshirt"), and sometimes lists a duo
-- ALONGSIDE its own members ("Madvillain, Madlib, MF DOOM"). Splitting
-- every such string is a parsing problem with no safe general rule (see
-- ARTIST_ALIASES' note on "Earth, Wind & Fire"), so it's not attempted here.
-- Instead: the credit stays one artist row, and this table records which
-- other artists (real people, or another act) are its members -- an
-- artist-to-artist fact, decoupled from any specific track/album. Curated
-- by hand (see ARTIST_MEMBERS in _shared.py), same spirit as ARTIST_ALIASES.
-- ============================================================
CREATE TABLE artist_members (
    artist_id   INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,  -- the group/collab act
    member_id   INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,  -- one of its members
    PRIMARY KEY (artist_id, member_id)
);
CREATE INDEX idx_artist_members_member ON artist_members(member_id);

-- ============================================================
-- Albums: RYM ratings export. Reuses the `artists` table from the concerts
-- domain -- same entity ("how many times have I seen X" and "what has X
-- released that I've rated" should join through one artists row, not two).
-- One row per album since the export is a ratings snapshot, not a time
-- series -- no separate rating-history table until there's evidence ratings
-- get revised and that history matters.
-- ============================================================
CREATE TABLE albums (
    id              INTEGER PRIMARY KEY,
    artist_id       INTEGER NOT NULL REFERENCES artists(id),
    title           TEXT NOT NULL,
    release_year    INTEGER,
    rating          INTEGER CHECK (rating BETWEEN 0 AND 10),
    rym_id          TEXT UNIQUE,        -- RateYourMusic's own album id, stable external key
    import_source_id INTEGER REFERENCES import_sources(id),
    date_added      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_albums_artist ON albums(artist_id);
CREATE INDEX idx_albums_rating ON albums(rating);

CREATE VIEW v_albums AS
SELECT al.id, ar.name AS artist, al.title, al.release_year, al.rating
FROM albums al JOIN artists ar ON ar.id = al.artist_id;

-- "Everything credited to X, directly OR as a member of a credited group"
-- -- e.g. Madlib's solo albums plus Freddie Gibbs & Madlib's.
CREATE VIEW v_albums_with_member_credits AS
SELECT al.id, ar.name AS artist, al.title, al.release_year, al.rating, 0 AS via_group
FROM albums al JOIN artists ar ON ar.id = al.artist_id
UNION ALL
SELECT al.id, m.name AS artist, al.title, al.release_year, al.rating, 1 AS via_group
FROM albums al
JOIN artist_members am ON am.artist_id = al.artist_id
JOIN artists m ON m.id = am.member_id;

-- ============================================================
-- Scrobbles: raw Last.fm play history. One row per play -- deliberately not
-- collapsed into per-artist play counts, since "what was I listening to in
-- a given month" and "how has an artist's play frequency trended" both need
-- the individual timestamps, not just a total. Shares `artists` with
-- concert_attendances/albums for the same reason those two do.
--
-- Track/album text lives on its own `tracks` row, not repeated per play --
-- 153k scrobbles collapse to ~30k distinct tracks (avg 5 plays/track), the
-- same repeated-value smell this file's normalization rule flags for
-- artists/exercises/foods. Missing album is stored as '' rather than NULL
-- so the UNIQUE constraint actually dedupes it (SQLite treats every NULL as
-- distinct in a unique index, which would silently let re-imports create a
-- fresh track row per play with no album tag).
-- ============================================================
CREATE TABLE tracks (
    id              INTEGER PRIMARY KEY,
    artist_id       INTEGER NOT NULL REFERENCES artists(id),
    title           TEXT NOT NULL,
    album           TEXT NOT NULL DEFAULT '',
    UNIQUE (artist_id, title, album)
);
CREATE INDEX idx_tracks_artist ON tracks(artist_id);

CREATE TABLE scrobbles (
    id              INTEGER PRIMARY KEY,
    track_id        INTEGER NOT NULL REFERENCES tracks(id),
    played_at       TEXT NOT NULL,      -- UTC, 'YYYY-MM-DDTHH:MM:SSZ'
    import_source_id INTEGER REFERENCES import_sources(id),
    UNIQUE (track_id, played_at)
);
CREATE INDEX idx_scrobbles_track ON scrobbles(track_id);
CREATE INDEX idx_scrobbles_played_at ON scrobbles(played_at);

CREATE VIEW v_scrobbles AS
SELECT sc.id, ar.name AS artist, tr.title AS track, NULLIF(tr.album, '') AS album, sc.played_at
FROM scrobbles sc
JOIN tracks tr ON tr.id = sc.track_id
JOIN artists ar ON ar.id = tr.artist_id;

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

CREATE VIEW v_facts AS
SELECT
    f.id, sub.name AS subject, f.statement, f.is_original_claim, f.is_personal,
    f.trust_level, f.trust_rationale, f.status, f.recheck_by, f.recheck_rationale, f.freshness, f.kind, f.valid_from, f.valid_to,
    vf.path AS origin_path, f.notes, f.date_added, f.last_reviewed_at
FROM facts f
JOIN subjects sub ON sub.id = f.subject_id
LEFT JOIN vault_files vf ON vf.id = f.origin_file_id;

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
