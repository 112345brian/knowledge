"""The normal tier's whitelist, the rules (domain: no sqlite, no files).

Which tables and columns the normal DB may hold, and the decisions about which rows are allowed
into it: a fact must be stored normal, sit on a subject chain with no private tag, and pass the
privacy rules on every text field that is copied. `normal_db_service` reads the full DB's
rows and hands them to `select_content`, which decides what is copied; `normal_db_store` (the SQLite adapter)
only reads and writes; `leak_rules` / `leak_test` check the result.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import privacy

NORMAL_DB_NAME = "knowledge-normal.db"

# Every table the normal DB may contain (FTS shadow tables are the 'fts' siblings below).
TABLES = ("build_info", "subjects", "source_identifiers", "subject_aliases", "entities", "entity_aliases", "fact_entities", "publishers", "authors", "sources", "source_authors", "facts",
          "fact_revisions", "fact_sources")
FTS_TABLES = ("facts_fts", "sources_fts", "authors_fts")
VIEWS = ("v_subjects",)

FACT_COLS = ("id, subject_id, statement, is_original_claim, trust_level, trust_rationale, status, "
             "superseded_by_fact_id, date_added, last_reviewed_at, recheck_by, recheck_rationale, "
             "visibility, source_key, freshness, kind, valid_from, valid_to, applies_to")
# FACT_COLS positions copied as free text: r[5] trust_rationale, r[11] recheck_rationale, r[18] applies_to
REVISION_COLS = ("id, fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, "
                 "trust_rationale, status, visibility, superseded_by, recheck_by, recheck_rationale, freshness, "
                 "kind, valid_from, valid_to, applies_to")
# REVISION_COLS positions copied as free text: r[8] trust_rationale, r[13] recheck_rationale, r[18] applies_to
SOURCE_COLS = "id, citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description, status, edition"
SOURCE_EDITION = 10  # SOURCE_COLS position of `edition`, free text that passes the keyword rules like the others


class NormalDbError(Exception):
    pass


def qmarks(n):
    return ",".join("?" * n)


def chain_ids(sid, parents):
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


class SubjectFilter:
    """Decides whether a row on subject `sid` may enter the normal DB. `names`, `parents`, `private` are
    by subject id (from the full DB); `rules` is a privacy.Rules."""

    def __init__(self, names, parents, private, rules):
        self.names, self.parents, self.private = names, parents, private
        self.ctx = rules.with_context(
            parents={names[i]: names.get(parents[i]) for i in names}, known_subjects=set(names.values()))

    def chain(self, sid):
        return chain_ids(sid, self.parents)

    def passes(self, sid, statement, visibility, extra=()):
        chain = self.chain(sid)
        if chain is None or any(self.private[i] for i in chain):
            return False
        return privacy.resolve_visibility(self.names[sid], statement, visibility, self.ctx, extra_text=extra).visibility == "normal"


def clean_text(text, rules):
    """`text` unless it is blank or contains a listed keyword (#43, #41, #42, #48): free text copied beside a
    fact (a subject description or alias, an edition, an entity's name or note, an `other` identifier) must
    pass the keyword rules like the statement; a failing text is dropped, the row it sits on stays."""
    return text if text and not privacy.match_keywords(text, rules.keywords) else None


def cite_text_index(rows):
    """{fact_id: [locator/quote text]} from (fact_id, locator, quote) rows. Free text that is COPIED into
    the normal DB besides the statement must pass the keyword rules too: the resolver only ever scanned
    the statement, so a listed name in a citation quote would otherwise slip into the remote tier."""
    out = {}
    for fid, locator, quote in rows:
        out.setdefault(fid, []).extend(t for t in (locator, quote) if t)
    return out


def select_facts(rows, flt, cite_text):
    """The FACT_COLS rows that may enter: normal, clean subject chain, and every copied text field passes."""
    return [r for r in rows if flt.passes(r[1], r[2], r[12], extra=(r[5], r[11], r[18], *cite_text.get(r[0], ())))]


def copy_fact_row(row, fact_ids):
    """The row as stored: superseded_by_fact_id is cleared when it names a fact that is not in the DB."""
    r = list(row)
    if r[7] not in fact_ids:
        r[7] = None
    return r


def select_revisions(rows, fact_ids, subject_of, flt, fact_keys):
    """REVISION_COLS rows of an included fact that are themselves normal and pass the rules, with
    `superseded_by` kept only when it names an included fact (by source_key)."""
    out = []
    for r in rows:
        if r[1] not in fact_ids or not flt.passes(subject_of[r[1]], r[6], r[10], extra=(r[8], r[13], r[18])):
            continue
        r = list(r)
        if r[11] not in fact_keys:
            r[11] = None
        out.append(r)
    return out


def check_objects(names, trigger_count):
    """Only whitelisted tables/views exist (FTS shadow tables allowed): no non-fact table, empty or not."""
    allowed = set(TABLES) | set(FTS_TABLES) | set(VIEWS)
    allowed |= {f"{f}_{s}" for f in FTS_TABLES for s in ("data", "idx", "docsize", "config")}
    extra = set(names) - allowed
    if extra:
        raise NormalDbError(f"unexpected objects in the normal DB: {sorted(extra)}")
    if trigger_count:
        raise NormalDbError("unexpected triggers in the normal DB")


def format_counts(counts):
    return "\n".join(f"  {t}: {n}" for t, n in counts.items())


# --------------------------------------------------------------------------- what is read, what is written

SUBJECT_COLS = ("id", "name", "domain", "parent_id", "description", "parent_relation", "deprecated", "replaced_by_subject_id")
SUBJECT_ALIAS_COLS = ("subject_id", "alias")
FACT_SOURCE_COLS = ("fact_id", "source_id", "locator", "quote")
PUBLISHER_COLS = ("id", "name")
AUTHOR_COLS = ("id", "name")
SOURCE_AUTHOR_COLS = ("source_id", "author_id", "author_order")
IDENTIFIER_COLS = ("source_id", "scheme", "value")
ENTITY_COLS = ("id", "entity_key", "canonical_name", "name_norm", "type", "private", "external_id", "notes")
ENTITY_ALIAS_COLS = ("entity_id", "alias", "alias_norm")
FACT_ENTITY_COLS = ("fact_id", "entity_id")
BUILD_INFO_COLS = ("id", "built_at", "schema_version")


@dataclass(frozen=True)
class FullDbRows:
    """What `select_content` needs from the full DB, as plain rows (the `NormalSource` port returns it; no
    connection crosses the port). Every list is in the order named; a column list is the SELECT's order.
      subjects          (id, name, parent_id, private, domain, description, parent_relation, deprecated, replaced_by)
      subject_aliases   (subject_id, alias) by subject_id, alias
      citations         (fact_id, source_id, locator, quote) by fact_id, source_id
      facts             FACT_COLS rows with visibility = 'normal', by id
      revisions         REVISION_COLS rows with visibility = 'normal', by id
      sources           SOURCE_COLS rows by id
      publishers        (id, name) by id
      authors           (id, name) by id
      source_authors    (source_id, author_id, author_order) by source_id, author_id
      identifiers       (source_id, scheme, value) by source_id, scheme, value
      entities          ENTITY_COLS rows, every entity with its `private` flag (the rules drop the private ones), by id
      entity_aliases    (entity_id, alias, alias_norm) by entity_id, alias
      fact_entities     (fact_id, entity_id) by fact_id, entity_id
      build_info        the latest (id, built_at, schema_version), or None
    """
    subjects: List[Tuple]
    subject_aliases: List[Tuple]
    citations: List[Tuple]
    facts: List[Tuple]
    revisions: List[Tuple]
    sources: List[Tuple]
    publishers: List[Tuple]
    authors: List[Tuple]
    source_authors: List[Tuple]
    identifiers: List[Tuple]
    entities: List[Tuple]
    entity_aliases: List[Tuple]
    fact_entities: List[Tuple]
    build_info: Optional[Tuple]


@dataclass(frozen=True)
class TableRows:
    """Rows to insert into one normal-DB table, in insertion order."""
    table: str
    columns: Tuple[str, ...]
    rows: List[Any]


@dataclass(frozen=True)
class Integrity:
    """What the sink reports about a written normal DB: foreign-key violations, row counts by table (TABLES),
    the names of its tables and views, and how many triggers it holds."""
    foreign_key_violations: List[Any]
    counts: Dict[str, int]
    object_names: Set[str]
    trigger_count: int


def select_content(data, rules):
    """The rows the normal DB gets from the full DB's `data` (a FullDbRows), as TableRows in insertion order.
    Everything the policy decides is here: the subject floor, the keyword rules on every copied text, which
    dangling references are cleared and which rows are dropped for lack of an included parent."""
    names = {r[0]: r[1] for r in data.subjects}
    parents = {r[0]: r[2] for r in data.subjects}
    private = {r[0]: r[3] for r in data.subjects}
    flt = SubjectFilter(names, parents, private, rules)
    cite_text = cite_text_index((fid, loc, quote) for fid, _sid, loc, quote in data.citations)

    def clean(text):
        return clean_text(text, rules)

    # 1. facts
    facts = select_facts(data.facts, flt, cite_text)
    fact_ids = {r[0] for r in facts}
    fact_keys, subject_ids, fact_rows = set(), set(), []
    for r in facts:
        r = copy_fact_row(r, fact_ids)
        subject_ids.update(flt.chain(r[1]))
        if r[13] is not None:
            fact_keys.add(r[13])
        fact_rows.append(r)

    # 2. subjects (used + ancestors); description and aliases pass the keyword rules (#43); a replacement that
    # is not in the DB is dropped, not dangled
    subject_rows, alias_rows = [], []
    by_id = {r[0]: r for r in data.subjects}
    for sid in sorted(subject_ids):
        _id, name, parent_id, _private, domain, desc, relation, deprecated, replaced = by_id[sid]
        subject_rows.append((sid, name, domain, parent_id, clean(desc), relation, deprecated,
                             replaced if replaced in subject_ids else None))
    for sid, alias in data.subject_aliases:
        if sid in subject_ids and clean(alias):
            alias_rows.append((sid, alias))

    # 3. revisions: of an included fact, only revisions that are themselves normal and pass the rules
    subject_of = {r[0]: r[1] for r in facts}
    revision_rows = select_revisions(data.revisions, fact_ids, subject_of, flt, fact_keys)

    # 4. citations of included facts, and only the sources they cite
    citation_rows = [tuple(r) for r in data.citations if r[0] in fact_ids]
    source_ids = {r[1] for r in citation_rows}
    src_rows = [r for r in data.sources if r[0] in source_ids]
    publisher_ids = {r[4] for r in src_rows if r[4] is not None}
    publisher_rows = [tuple(r) for r in data.publishers if r[0] in publisher_ids]
    source_out = []
    for r in src_rows:
        r = list(r)
        r[SOURCE_EDITION] = clean(r[SOURCE_EDITION])  # #41: edition is free text, so it passes the keyword rules
        source_out.append(r)
    sa = [tuple(r) for r in data.source_authors if r[0] in source_ids]
    author_ids = {r[1] for r in sa}
    author_rows = [tuple(r) for r in data.authors if r[0] in author_ids]

    # 4a. identifiers (#48) of the included sources; an `other` value is free text and passes the keyword rules
    in_sources = {r[0] for r in src_rows}
    identifier_rows = [tuple(r) for r in data.identifiers
                       if r[0] in in_sources and (r[1] != "other" or clean(r[2]) is not None)]

    # 4b. entities (#42): never a private entity (the flag is read with the row; anything but 0 counts as private),
    # only those an INCLUDED fact mentions, and only if the name passes the keyword rules; an alias, notes or
    # external_id with a listed keyword is dropped
    entity_by_id = {r[0]: r for r in data.entities if r[5] == 0}
    linked = {}
    for fid, eid in data.fact_entities:
        if fid in fact_ids and eid in entity_by_id:
            linked.setdefault(eid, []).append(fid)
    aliases_of = {}
    for eid, alias, alias_norm in data.entity_aliases:
        aliases_of.setdefault(eid, []).append((eid, alias, alias_norm))
    entity_rows, entity_alias_rows, link_rows = [], [], []
    for eid in sorted(linked):
        _, key, name, norm, etype, _private, external_id, notes = entity_by_id[eid]
        if clean(name) is None or clean(norm) is None:
            continue
        entity_rows.append((eid, key, name, norm, etype, 0, clean(external_id), clean(notes)))
        for _e, alias, alias_norm in aliases_of.get(eid, ()):
            if clean(alias) is not None and clean(alias_norm) is not None:
                entity_alias_rows.append((eid, alias, alias_norm))
        for fid in linked[eid]:
            link_rows.append((fid, eid))

    # 4c. build info (#47): when and under which schema version only
    build_rows = [tuple(data.build_info)] if data.build_info else []

    def cols(s):
        return tuple(c.strip() for c in s.split(","))

    return [
        TableRows("facts", cols(FACT_COLS), fact_rows),
        TableRows("subjects", SUBJECT_COLS, subject_rows),
        TableRows("subject_aliases", SUBJECT_ALIAS_COLS, alias_rows),
        TableRows("fact_revisions", cols(REVISION_COLS), revision_rows),
        TableRows("publishers", PUBLISHER_COLS, publisher_rows),
        TableRows("sources", cols(SOURCE_COLS), source_out),
        TableRows("authors", AUTHOR_COLS, author_rows),
        TableRows("source_authors", SOURCE_AUTHOR_COLS, sa),
        TableRows("fact_sources", FACT_SOURCE_COLS, citation_rows),
        TableRows("source_identifiers", IDENTIFIER_COLS, identifier_rows),
        TableRows("entities", ENTITY_COLS, entity_rows),
        TableRows("entity_aliases", ENTITY_ALIAS_COLS, entity_alias_rows),
        TableRows("fact_entities", FACT_ENTITY_COLS, link_rows),
        TableRows("build_info", BUILD_INFO_COLS, build_rows),
    ]


def verify_integrity(integrity):
    """Raise NormalDbError unless the written DB has no dangling reference and holds only whitelisted objects."""
    if integrity.foreign_key_violations:
        raise NormalDbError(f"foreign key violations in the normal DB: {integrity.foreign_key_violations[:3]}")
    check_objects(integrity.object_names, integrity.trigger_count)
