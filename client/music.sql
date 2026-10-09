-- Optional client source `music` (CLIENT_SOURCES in local_paths.py): concerts, album ratings and scrobbles --
-- artists, venues, festivals, attendances, albums, tracks and the files they were imported from. Applied
-- after schema.sql by build.py only when the source is enabled; schema.sql knows nothing about these tables.

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

-- Ingest scripts resolve artist identity case-insensitively in Python (see
-- get_or_create_artist in client/music_shared.py) before ever inserting -- this index is
-- the backstop that turns a logic bug into a build failure instead of a
-- silent duplicate ("JPEGMAFIA" row 12 vs "Jpegmafia" row 340).
CREATE UNIQUE INDEX idx_artists_name_nocase ON artists(lower(name));
