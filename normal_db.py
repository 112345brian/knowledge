#!/usr/bin/env python3
"""Build knowledge-normal.db: a copy of the full DB that CANNOT contain private data (#21).

Used so a remote-served connector never has anything private to leak. The build COPIES a
whitelist INTO a fresh, empty database; it never deletes from a copy of knowledge.db (deletion
leaves old pages, free-list garbage and FTS shadow rows holding the deleted text).

What goes in (everything else is absent, not empty):
  facts            only visibility='normal', on a non-private subject chain, and not caught by the
                   privacy rules passed in (re-checked here, so a rule added after the last ingest
                   still keeps a fact out). Columns: id, subject_id, statement, is_original_claim,
                   trust_level, trust_rationale, status, superseded_by_fact_id (NULL unless that
                   fact is also in), date_added, last_reviewed_at, recheck_by, recheck_rationale,
                   visibility, source_key, freshness (#7; staleness metadata like recheck_by, not
                   private: without it a NULL recheck_by cannot be told from "never reviewed").
                   LEFT OUT on purpose: is_personal (a keyword heuristic that can hint at private
                   topics), notes (free text), origin_file_id (vault path), provided_by,
                   captured_via, session_id, captured_at, source_quote (words from a conversation).
  fact_revisions   revisions of an included fact whose OWN visibility is normal and which pass the
                   rules (an older revision that was private is dropped, leaving a gap in the
                   revision numbers; a fact whose current visibility is private has no history
                   here at all). LEFT OUT: session_id, change_reason, notes. `superseded_by` is
                   kept only when it names an included fact.
  fact_sources     citation rows of included facts only. `locator` and `quote` ARE included: they
                   belong to a normal fact, so they are as visible as the fact. A quote on a
                   private fact's citation of the same source is not copied.
  sources          only sources cited by an included fact (name, citekey, type, url, dates,
                   description, status and edition (#41)). LEFT OUT: origin_path (vault file path), created_at, status_date,
                   status_note (may hold a private URL), original_published_date, source_relations, the custodial-history
                   columns (acquired_at, acquired_via, where_from, acquired_note: a download URL reveals interests, #46)
                   and the fixity columns
                   (content_sha256, size_bytes, file_mtime, mime_type, file_state; #38).
  authors, source_authors, publishers   only those reachable from an included source.
  subjects         subjects used by included facts plus their parent chain (id, name, domain,
                   parent_id). LEFT OUT: the `private` flag column (all are non-private).
  facts_fts, sources_fts, authors_fts   rebuilt over the copied rows.
  v_subjects       view: subjects having at least one included fact, with a count of INCLUDED
                   facts only (never stored, so it cannot go stale or count private facts).
subjects        also description, parent_relation, deprecated, replaced_by_subject_id (only if that subject is
                   included) and subject_aliases (#43); a description or alias that contains a listed keyword
                   is dropped, the subject stays.
entities        only NON-private entities that an included fact mentions (key, name, type, external_id, notes), their
                   aliases and the fact_entities links of included facts (#42). A private entity, its aliases, notes
                   and links are never copied; a name, alias or note containing a listed keyword is dropped.
build_info      the latest row's built_at and schema_version only (#47); commits, dirty flags, versions and build_inputs
                   (input keys) are never copied.
source_identifiers   identifiers of included sources (#48); source_identifier_conflicts is never copied.
Never copied: claims (and their fact links), vault_files, metrics, measurements, fact_measurements,
exercises, training_sets, foods, food/meal logs, muscles, import_sources, artists, venues,
festivals, concert_attendances, artist_members, albums, tracks, scrobbles, and the views/triggers
that sit on them.
"""
import os
import sqlite3
import sys
import tempfile

import privacy

NORMAL_DB_NAME = "knowledge-normal.db"

# Every table the normal DB may contain (FTS shadow tables are the 'fts' siblings below).
TABLES = ("build_info", "subjects", "source_identifiers", "subject_aliases", "entities", "entity_aliases", "fact_entities", "publishers", "authors", "sources", "source_authors", "facts",
          "fact_revisions", "fact_sources")
FTS_TABLES = ("facts_fts", "sources_fts", "authors_fts")
VIEWS = ("v_subjects",)

DDL = """
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


class NormalDbError(Exception):
    pass


def _qmarks(n):
    return ",".join("?" * n)


def _subject_context(full):
    """name by id, parent id by id, private flag by id, from the full DB."""
    names, parents, private = {}, {}, {}
    for sid, name, pid, priv in full.execute("SELECT id, name, parent_id, private FROM subjects"):
        names[sid], parents[sid], private[sid] = name, pid, priv
    return names, parents, private


def _chain_ids(sid, parents):
    """sid and its ancestors; None if the tree loops or dangles (fail closed)."""
    out, seen = [], set()
    cur = sid
    while cur is not None:
        if cur in seen or cur not in parents:
            return None
        seen.add(cur)
        out.append(cur)
        cur = parents[cur]
    return out


def populate(out, full, rules):
    """Copy the whitelist from `full` (a read-only connection) into `out` (fresh, schema made)."""
    names, parents, private = _subject_context(full)
    ctx = rules.with_context(
        parents={names[i]: names.get(parents[i]) for i in names}, known_subjects=set(names.values()))

    # Free text that is COPIED into this DB besides the statement must pass the keyword rules too:
    # the resolver only ever scanned the statement, so a listed name in a rationale or a citation
    # quote would otherwise slip through into the tier that is served remotely.
    cite_text = {}
    for fid, locator, quote in full.execute("SELECT fact_id, locator, quote FROM fact_sources"):
        cite_text.setdefault(fid, []).extend(t for t in (locator, quote) if t)

    def passes(sid, statement, visibility, extra=()):
        chain = _chain_ids(sid, parents)
        if chain is None or any(private[i] for i in chain):
            return False
        return privacy.resolve_visibility(names[sid], statement, visibility, ctx, extra_text=extra).visibility == "normal"

    # 1. facts
    fact_keys, subject_ids = set(), set()
    cols = ("id, subject_id, statement, is_original_claim, trust_level, trust_rationale, status, "
            "superseded_by_fact_id, date_added, last_reviewed_at, recheck_by, recheck_rationale, "
            "visibility, source_key, freshness, kind, valid_from, valid_to, applies_to")
    # cols: r[5] trust_rationale, r[11] recheck_rationale, r[18] applies_to (all copied, so all privacy-scanned)
    facts = [r for r in full.execute(f"SELECT {cols} FROM facts WHERE visibility = 'normal' ORDER BY id")
             if passes(r[1], r[2], r[12], extra=(r[5], r[11], r[18], *cite_text.get(r[0], ())))]
    fact_ids = {r[0] for r in facts}
    for r in facts:
        r = list(r)
        if r[7] not in fact_ids:
            r[7] = None  # superseded_by points at a fact that is not in this DB
        subject_ids.update(_chain_ids(r[1], parents))
        if r[13] is not None:
            fact_keys.add(r[13])
        out.execute(f"INSERT INTO facts ({cols}) VALUES ({_qmarks(19)})", r)

    # 2. subjects (used + ancestors), parents first is not required (FKs are checked at commit)
    # #43: description, relation label, deprecation and aliases come along, but the free text (description,
    # aliases) must pass the keyword rules like every other copied text: a listed name in either drops
    # that text (the subject itself stays). A replacement that is not in this DB is dropped, not dangled.
    def clean_text(text):
        return text if text and not privacy.match_keywords(text, rules.keywords) else None

    for sid in sorted(subject_ids):
        domain, parent_id, desc, relation, deprecated, replaced = full.execute(
            "SELECT domain, parent_id, description, parent_relation, deprecated, replaced_by_subject_id "
            "FROM subjects WHERE id = ?", (sid,)).fetchone()
        out.execute("INSERT INTO subjects (id, name, domain, parent_id, description, parent_relation, deprecated, "
                    "replaced_by_subject_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, names[sid], domain, parent_id, clean_text(desc), relation, deprecated,
                     replaced if replaced in subject_ids else None))
        for (alias,) in full.execute("SELECT alias FROM subject_aliases WHERE subject_id = ? ORDER BY alias", (sid,)):
            if clean_text(alias):
                out.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (?, ?)", (sid, alias))

    # 3. revisions: of an included fact, only revisions that are themselves normal and pass the rules
    rcols = ("id, fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, "
             "trust_rationale, status, visibility, superseded_by, recheck_by, recheck_rationale, freshness, kind, valid_from, valid_to, applies_to")
    subject_of = {r[0]: r[1] for r in facts}
    for r in full.execute(f"SELECT {rcols} FROM fact_revisions WHERE visibility = 'normal' ORDER BY id"):
        # rcols: r[8] trust_rationale, r[13] recheck_rationale, r[18] applies_to
        if r[1] not in fact_ids or not passes(subject_of[r[1]], r[6], r[10], extra=(r[8], r[13], r[18])):
            continue
        r = list(r)
        if r[11] not in fact_keys:
            r[11] = None
        out.execute(f"INSERT INTO fact_revisions ({rcols}) VALUES ({_qmarks(19)})", r)

    # 4. citations of included facts, and only the sources they cite
    source_ids = set()
    for fid, sid, locator, quote in full.execute(
            "SELECT fact_id, source_id, locator, quote FROM fact_sources ORDER BY fact_id, source_id"):
        if fid in fact_ids:
            source_ids.add(sid)
    publishers, authors = set(), set()
    scols = "id, citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, status, edition"
    src_rows = [r for r in full.execute(f"SELECT {scols} FROM sources ORDER BY id") if r[0] in source_ids]
    for r in src_rows:
        if r[4] is not None:
            publishers.add(r[4])
    for pid, name in full.execute("SELECT id, name FROM publishers ORDER BY id"):
        if pid in publishers:
            out.execute("INSERT INTO publishers (id, name) VALUES (?, ?)", (pid, name))
    for r in src_rows:
        r = list(r)
        r[10] = clean_text(r[10])        # #41: edition is free text, so it passes the keyword rules like the others
        out.execute(f"INSERT INTO sources ({scols}) VALUES ({_qmarks(11)})", r)
    sa = [r for r in full.execute("SELECT source_id, author_id, author_order FROM source_authors ORDER BY source_id, author_id")
          if r[0] in source_ids]
    authors = {r[1] for r in sa}
    for aid, name in full.execute("SELECT id, name FROM authors ORDER BY id"):
        if aid in authors:
            out.execute("INSERT INTO authors (id, name) VALUES (?, ?)", (aid, name))
    for r in sa:
        out.execute("INSERT INTO source_authors (source_id, author_id, author_order) VALUES (?, ?, ?)", r)
    for fid, sid, locator, quote in full.execute(
            "SELECT fact_id, source_id, locator, quote FROM fact_sources ORDER BY fact_id, source_id"):
        if fid in fact_ids:
            out.execute("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (?, ?, ?, ?)",
                        (fid, sid, locator, quote))

    # 4a. identifiers (#48) of the included sources: public references (DOI, ISBN, ...), so they come along; an
    # `other` value is free text and passes the keyword rules first. Conflicts are build diagnostics, never copied.
    in_sources = {r[0] for r in src_rows}
    for sid, scheme, value in full.execute("SELECT source_id, scheme, value FROM source_identifiers ORDER BY source_id, scheme, value"):
        if sid in in_sources and (scheme != "other" or clean_text(value) is not None):
            out.execute("INSERT INTO source_identifiers (source_id, scheme, value) VALUES (?, ?, ?)", (sid, scheme, value))

    # 4b. entities (#42): never a private entity, its aliases or its links. A non-private entity comes along
    # only when an INCLUDED fact mentions it, and only if its name passes the keyword rules (a listed name
    # in the name itself would expose it); an alias or notes/external_id containing a listed keyword is dropped.
    # A private entity's links cannot be in an included fact (the resolver made that fact private), but they
    # are excluded here on their own account too, so the two protections are independent.
    entity_rows = {r[0]: r for r in full.execute(
        "SELECT id, entity_key, canonical_name, name_norm, type, external_id, notes FROM entities WHERE private = 0 ORDER BY id")}
    linked = {}
    for fid, eid in full.execute("SELECT fact_id, entity_id FROM fact_entities ORDER BY fact_id, entity_id"):
        if fid in fact_ids and eid in entity_rows:
            linked.setdefault(eid, []).append(fid)
    for eid in sorted(linked):
        _, key, name, norm, etype, external_id, notes = entity_rows[eid]
        if clean_text(name) is None or clean_text(norm) is None:
            continue
        out.execute("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type, private, external_id, notes) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?)", (eid, key, name, norm, etype, clean_text(external_id), clean_text(notes)))
        for alias, alias_norm in full.execute("SELECT alias, alias_norm FROM entity_aliases WHERE entity_id = ? ORDER BY alias", (eid,)):
            if clean_text(alias) is not None and clean_text(alias_norm) is not None:
                out.execute("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (?, ?, ?)", (eid, alias, alias_norm))
        for fid in linked[eid]:
            out.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fid, eid))

    # 4c. build info (#47): when and under which schema version only; commits, versions and input keys stay out
    row = full.execute("SELECT id, built_at, schema_version FROM build_info ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        out.execute("INSERT INTO build_info (id, built_at, schema_version) VALUES (?, ?, ?)", tuple(row))

    # 5. FTS over what was copied
    for fts in FTS_TABLES:
        out.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")


def build_normal_db(full_path, target_path, rules):
    """Write a fresh normal DB at exactly `target_path` (must not exist). `rules` is a
    privacy.Rules (pass privacy.Rules() for none). Returns {table: row count}."""
    if os.path.exists(target_path):
        raise NormalDbError(f"{target_path} already exists; build into a fresh path")
    full = sqlite3.connect(f"file:{os.path.abspath(full_path)}?mode=ro", uri=True)
    out = sqlite3.connect(target_path)
    try:
        out.execute("PRAGMA journal_mode = DELETE")
        out.executescript(DDL)
        populate(out, full, rules)
        out.commit()
        bad = out.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise NormalDbError(f"foreign key violations in the normal DB: {bad[:3]}")
        counts = table_counts(out)
        check_structure(out)
    finally:
        out.close()
        full.close()
    return counts


def table_counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def check_structure(con):
    """Only whitelisted tables/views exist (FTS shadow tables allowed): no non-fact table, empty or not."""
    allowed = set(TABLES) | set(FTS_TABLES) | set(VIEWS)
    allowed |= {f"{f}_{s}" for f in FTS_TABLES for s in ("data", "idx", "docsize", "config")}
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    extra = names - allowed
    if extra:
        raise NormalDbError(f"unexpected objects in the normal DB: {sorted(extra)}")
    if con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0]:
        raise NormalDbError("unexpected triggers in the normal DB")


def remove_db_files(path):
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def build_normal_atomic(full_path, directory, rules, leak_check=True):
    """Build into a temp file in `directory`, leak-check it against the full DB, then
    os.replace onto knowledge-normal.db. On any failure the temp file is removed AND a previous
    knowledge-normal.db is removed too: a stale normal DB could still hold a fact that has since
    been made private, so no file is safer than an out-of-date one. Returns (final_path, counts)."""
    import leak_test  # local: leak_test imports this module for the fixture
    final = os.path.join(directory, NORMAL_DB_NAME)
    fd, tmp = tempfile.mkstemp(prefix=NORMAL_DB_NAME + ".building-", dir=directory)
    os.close(fd)
    os.remove(tmp)  # sqlite creates it fresh
    try:
        counts = build_normal_db(full_path, tmp, rules)
        if leak_check:
            leaks = leak_test.scan_against_full(tmp, full_path)
            if leaks:
                raise NormalDbError("leak test failed:\n" + leak_test.format_leaks(leaks))
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, final)
    except BaseException:
        remove_db_files(tmp)
        remove_db_files(final)
        raise
    return final, counts


def format_counts(counts):
    return "\n".join(f"  {t}: {n}" for t, n in counts.items())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: normal_db.py FULL_DB OUT_DIR   (builds OUT_DIR/knowledge-normal.db)", file=sys.stderr)
        return 2
    try:
        rules = privacy.load_rules()
        path, counts = build_normal_atomic(argv[0], argv[1], rules)
    except Exception as e:
        print(f"error: normal DB build failed: {e}", file=sys.stderr)
        return 1
    print(f"Built {path}\n{format_counts(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
