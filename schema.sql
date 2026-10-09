-- knowledge.db canonical schema (the core).
-- Optional client sources add their own tables from client/*.sql, applied after this file by build.py only when
-- the source is enabled (CLIENT_SOURCES in local_paths.py). Nothing in this file refers to those tables.
-- This file is the single source of truth for the core structure. knowledge.db itself
-- is a BUILD ARTIFACT of this schema plus the ingest_*.py / seed_*.py scripts
-- in this directory, run in order by build.py. Never hand-edit the .db file
-- or run ad hoc ALTER/INSERT against it -- change a script here and rebuild.
--
-- Normalization rule of thumb applied throughout: if a column's values repeat
-- across rows and you'd ever ask "how many/which/all" about those values as a
-- group, it's an entity with its own table (subjects, authors, metrics,
-- artists/venues/festivals) -- never a repeated text field.

PRAGMA foreign_keys = ON;

-- Schema version (#47): bump this whenever a table, column, constraint, view or trigger below changes.
-- The build records it in build_info; tests/test_build_info.py fails when the schema changed without a bump.
PRAGMA user_version = 3;

-- Which inputs and code produced this db (#47); written by the build, see build_info.py. input_key is a stable
-- name, never a path.
CREATE TABLE build_info (
    id              INTEGER PRIMARY KEY,
    built_at        TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    code_commit     TEXT,
    code_dirty      INTEGER CHECK (code_dirty IS NULL OR code_dirty IN (0,1)),
    private_commit  TEXT,
    private_dirty   INTEGER CHECK (private_dirty IS NULL OR private_dirty IN (0,1)),
    python_version  TEXT,
    sqlite_version  TEXT
);
CREATE TABLE build_inputs (
    build_id    INTEGER NOT NULL REFERENCES build_info(id) ON DELETE CASCADE,
    input_key   TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('present','missing')),
    sha256      TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    size_bytes  INTEGER,
    file_mtime  TEXT,
    read_at     TEXT NOT NULL,
    PRIMARY KEY (build_id, input_key)
);

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
    private     INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0,1)),
    -- #43, all loaded from subjects.json by step 06. `parent_id` stays the SINGLE inheritance path for
    -- privacy tags (#31); `parent_relation` only labels what that edge means (broader | part-of |
    -- subtype-of | member-of). A deprecated subject is refused for new facts (existing facts keep it);
    -- `replaced_by_subject_id` names the subject to use instead.
    description            TEXT,
    parent_relation        TEXT NOT NULL DEFAULT 'broader' CHECK (parent_relation IN ('broader','part-of','subtype-of','member-of')),
    deprecated             INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),
    replaced_by_subject_id INTEGER REFERENCES subjects(id),
    CHECK (replaced_by_subject_id IS NULL OR deprecated = 1),
    CHECK (replaced_by_subject_id IS NULL OR replaced_by_subject_id <> id)
);

-- Alternative names for a subject (#43). A fact filed under an alias is stored under the canonical
-- subject. An alias may never equal a subject name, in either direction (the triggers below).
CREATE TABLE subject_aliases (
    subject_id  INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL UNIQUE,
    PRIMARY KEY (subject_id, alias)
);
CREATE TRIGGER subject_aliases_not_a_subject BEFORE INSERT ON subject_aliases
WHEN EXISTS (SELECT 1 FROM subjects WHERE name = NEW.alias)
BEGIN SELECT RAISE(ABORT, 'alias collides with a subject name'); END;
CREATE TRIGGER subjects_not_an_alias BEFORE INSERT ON subjects
WHEN EXISTS (SELECT 1 FROM subject_aliases WHERE alias = NEW.name)
BEGIN SELECT RAISE(ABORT, 'subject name collides with an alias'); END;

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
    -- Status over time (#41). A plain field read from the vault note frontmatter (02) or manual_sources.json
    -- (01); git is its history (no revision log for sources, see source_status.py for what would change that).
    -- Legacy sources are 'active' with a NULL status_date: no date is ever invented. status_note may hold a
    -- private URL, so the normal-only DB leaves it out. original_published_date is the work's first
    -- publication; published_date is this edition's.
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','corrected','expression-of-concern','retracted','superseded')),
    status_date             TEXT CHECK (status_date IS NULL OR status_date GLOB '[0-9][0-9][0-9][0-9]' OR status_date GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR status_date GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    status_note             TEXT,
    edition                 TEXT,
    original_published_date TEXT,
    -- File fixity (#38): the file as it was when the build read it. NULL hash/size/mtime with
    -- file_state = 'missing' when the file was absent or unreadable; never an invented hash.
    content_sha256  TEXT CHECK (content_sha256 IS NULL OR length(content_sha256) = 64),
    size_bytes      INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    file_mtime      TEXT,
    mime_type       TEXT,
    file_state      TEXT CHECK (file_state IS NULL OR file_state IN ('present','missing')),
    -- Custodial history (#46): when and how the file came into the collection. Values from the data come first;
    -- the macOS file attributes only fill gaps and are marked in acquired_note (see acquisition.py). NULL =
    -- not recorded. where_from URLs can reveal private interests, so none of this reaches the normal-only DB.
    acquired_at     TEXT,
    acquired_via    TEXT CHECK (acquired_via IS NULL OR acquired_via IN ('download','manual','export','unknown')),
    where_from      TEXT,
    acquired_note   TEXT
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

-- "source_id <relation> related_source_id" (#41): `replaces` (the second is superseded) and `is-version-of`.
-- Directed, never reflexive; cycles are refused by source_status.check_relations at build time.
CREATE TABLE source_relations (
    source_id          INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    relation           TEXT NOT NULL CHECK (relation IN ('replaces','is-version-of')),
    related_source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    PRIMARY KEY (source_id, relation, related_source_id),
    CHECK (source_id <> related_source_id)
);
CREATE INDEX idx_source_relations_related ON source_relations(related_source_id);

-- Persistent identifiers (#48), normalized by identifiers.py (lowercase DOI, ISBN-13 with a verified check
-- digit, ...). One (scheme, value) belongs to exactly one source. When a second source claims the same
-- identifier it is NOT merged and NOT silently dropped: the claim is kept in source_identifier_conflicts
-- (with the owner) and reported at build time.
CREATE TABLE source_identifiers (
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    scheme     TEXT NOT NULL CHECK (scheme IN ('doi','isbn','issn','pmid','arxiv','other')),
    value      TEXT NOT NULL CHECK (length(trim(value)) > 0),
    PRIMARY KEY (source_id, scheme, value),
    UNIQUE (scheme, value)
);
CREATE TABLE source_identifier_conflicts (
    source_id        INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    scheme           TEXT NOT NULL,
    value            TEXT NOT NULL,
    owner_source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    PRIMARY KEY (source_id, scheme, value)
);

CREATE VIEW v_sources AS
SELECT s.id, s.citekey, s.name, s.source_type,
       GROUP_CONCAT(a.name, '; ') AS authors,
       p.name AS publisher, s.url, s.published_date, s.origin_path, s.status, s.edition
FROM sources s
LEFT JOIN source_authors sa ON sa.source_id = s.id
LEFT JOIN authors a ON a.id = sa.author_id
LEFT JOIN publishers p ON p.id = s.publisher_id
GROUP BY s.id;

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
    file_state      TEXT CHECK (file_state IS NULL OR file_state IN ('present','missing')),
    -- Custodial history (#46), same columns as sources; filled from the macOS file attributes only (a vault
    -- file has no data record of its own), marked in acquired_note. NULL = not recorded.
    acquired_at     TEXT,
    acquired_via    TEXT CHECK (acquired_via IS NULL OR acquired_via IN ('download','manual','export','unknown')),
    where_from      TEXT,
    acquired_note   TEXT
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
    -- Applicability (#45): who or what the fact applies to ("adult men", "type 2 diabetes"), short free
    -- text taken from the source or the user, never inferred. NULL = unknown/unstated, which is NOT the
    -- same as an explicit "general". Blank text is normalized to NULL by the writers. Mutable via a
    -- revision (#30) and part of the facts FTS.
    applies_to              TEXT CHECK (applies_to IS NULL OR length(trim(applies_to, char(32, 9, 10, 11, 12, 13))) > 0),
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
    applies_to       TEXT CHECK (applies_to IS NULL OR length(trim(applies_to, char(32, 9, 10, 11, 12, 13))) > 0),
    valid_to         TEXT CHECK (valid_to IS NULL OR valid_to GLOB '[0-9][0-9][0-9][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]' OR valid_to GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    notes            TEXT,
    CHECK (valid_from IS NULL OR valid_to IS NULL OR substr(valid_from, 1, min(length(valid_from), length(valid_to))) <= substr(valid_to, 1, min(length(valid_from), length(valid_to)))),
    UNIQUE (source_key, revision)
);
CREATE INDEX idx_fact_revisions_fact ON fact_revisions(fact_id, revision);
-- ===== END #30 fact revisions =====

-- ============================================================
-- Entities (#42): the people, organizations, places, projects and substances facts are about, loaded
-- from entities.json by step 13. A fact is linked to an entity when the entity's name or an alias appears
-- in the fact's text as a whole word (literal, case-insensitive; see entities.py). A `private` entity makes
-- every fact that mentions it private (privacy.py); the normal-only DB leaves private entities out.
-- ============================================================
CREATE TABLE entities (
    id              INTEGER PRIMARY KEY,
    entity_key      TEXT NOT NULL UNIQUE,   -- the stable slug from entities.json
    canonical_name  TEXT NOT NULL,
    name_norm       TEXT NOT NULL UNIQUE,   -- normalized (NFC, casefold, whitespace collapsed): what is matched
    type            TEXT NOT NULL CHECK (type IN ('person','organization','place','project','substance','other')),
    private         INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0,1)),
    external_id     TEXT,
    notes           TEXT
);
CREATE TABLE entity_aliases (
    entity_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL,
    alias_norm  TEXT NOT NULL UNIQUE,
    PRIMARY KEY (entity_id, alias)
);
CREATE TRIGGER entity_alias_not_a_name BEFORE INSERT ON entity_aliases
WHEN EXISTS (SELECT 1 FROM entities WHERE name_norm = NEW.alias_norm)
BEGIN SELECT RAISE(ABORT, 'alias collides with an entity name'); END;
CREATE TRIGGER entity_name_not_an_alias BEFORE INSERT ON entities
WHEN EXISTS (SELECT 1 FROM entity_aliases WHERE alias_norm = NEW.name_norm)
BEGIN SELECT RAISE(ABORT, 'entity name collides with an alias'); END;
CREATE TABLE fact_entities (
    fact_id    INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (fact_id, entity_id)
);
CREATE INDEX idx_fact_entities_entity ON fact_entities(entity_id);

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
    inference_type TEXT CHECK (inference_type IN ('deductive','inductive','abductive')),
    -- Toulmin structure (#44), both optional and forward-only like inference_type. warrant: WHY the cited
    -- grounds support the claim. qualifier: how far the claim holds ("usually", "in adults"). Blank text is
    -- stored as NULL by the seeding step, and the CHECKs refuse it.
    warrant   TEXT CHECK (warrant IS NULL OR length(trim(warrant, char(32, 9, 10, 11, 12, 13))) > 0),
    qualifier TEXT CHECK (qualifier IS NULL OR length(trim(qualifier, char(32, 9, 10, 11, 12, 13))) > 0)
);

CREATE TABLE claim_facts (
    claim_id    INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    fact_id     INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    -- The fact's part in the argument (#44): grounds (default; what existing rows are), backing (supports the
    -- warrant) or rebuttal (counter-evidence). A stale grounds/backing fact weakens the claim; a stale
    -- rebuttal only strengthens it, so claims_audit reports it as information. One role per (claim, fact);
    -- the same fact may be grounds for one claim and a rebuttal of another. note: why this fact is linked.
    role        TEXT NOT NULL DEFAULT 'grounds' CHECK (role IN ('grounds','backing','rebuttal')),
    note        TEXT CHECK (note IS NULL OR length(trim(note, char(32, 9, 10, 11, 12, 13))) > 0),
    PRIMARY KEY (claim_id, fact_id)
);

-- One row per (claim, cited fact) where the fact is superseded or retracted. Claims have no status of
-- their own, so every claim counts as active. The "past recheck_by" case is NOT here: recheck_by is
-- free text that is only sometimes a date, so claims_audit.audit_claims() does that part in Python.
CREATE VIEW v_claims_with_stale_premises AS
SELECT
    c.id AS claim_id, c.statement AS claim_statement, c.inference_type, cf.role,
    f.id AS fact_id, f.statement AS fact_statement, f.status AS reason,
    f.superseded_by_fact_id, f.trust_rationale, f.notes AS fact_notes
FROM claims c
JOIN claim_facts cf ON cf.claim_id = c.id
JOIN facts f ON f.id = cf.fact_id
WHERE f.status IN ('superseded','retracted');

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
    f.trust_level, f.trust_rationale, f.status, f.recheck_by, f.recheck_rationale, f.freshness, f.kind, f.valid_from, f.valid_to, f.applies_to,
    vf.path AS origin_path, f.notes, f.date_added, f.last_reviewed_at
FROM facts f
JOIN subjects sub ON sub.id = f.subject_id
LEFT JOIN vault_files vf ON vf.id = f.origin_file_id;

-- ============================================================
-- Full-text search over facts (requires an FTS5-enabled sqlite3 -- Python's
-- built-in module has it; this machine's `sqlite3` CLI does not).
-- ============================================================
CREATE VIRTUAL TABLE facts_fts USING fts5(
  statement, trust_rationale, notes, applies_to, content='facts', content_rowid='id'
);
CREATE TRIGGER facts_fts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, statement, trust_rationale, notes, applies_to) VALUES (new.id, new.statement, new.trust_rationale, new.notes, new.applies_to);
END;
CREATE TRIGGER facts_fts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, statement, trust_rationale, notes, applies_to) VALUES ('delete', old.id, old.statement, old.trust_rationale, old.notes, old.applies_to);
END;
CREATE TRIGGER facts_fts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, statement, trust_rationale, notes, applies_to) VALUES ('delete', old.id, old.statement, old.trust_rationale, old.notes, old.applies_to);
  INSERT INTO facts_fts(rowid, statement, trust_rationale, notes, applies_to) VALUES (new.id, new.statement, new.trust_rationale, new.notes, new.applies_to);
END;
