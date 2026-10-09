-- Optional client source `measurements` (CLIENT_SOURCES in local_paths.py): the structured numeric readings
-- of a health/fitness vault -- metrics, measurements, training sets, food and meal logs, muscle volume --
-- and the fact <-> measurement link. Applied after schema.sql by build.py only when the source is enabled;
-- schema.sql knows nothing about these tables.

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

CREATE VIEW measurement_with_source AS
SELECT
    m.id AS measurement_id, sub.name AS subject, met.key AS metric, met.label AS metric_label,
    m.value, met.unit, m.measured_at,
    m.trust_level, s.name AS source_name, s.source_type
FROM measurements m
JOIN subjects sub ON sub.id = m.subject_id
JOIN metrics met ON met.id = m.metric_id
JOIN sources s ON s.id = m.source_id;
