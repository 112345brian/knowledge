"""The normal tier's whitelist, the rules (domain: no sqlite, no files).

Which tables and columns the normal DB may hold, and the decisions about which rows are allowed
into it: a fact must be stored normal, sit on a subject chain with no private tag, and pass the
privacy rules on every text field that is copied. `normal_db` (the adapter) runs the SQL and the
file handling and applies these; `leak_rules` / `leak_test` check the result.
"""
import privacy

NORMAL_DB_NAME = "knowledge-normal.db"

# Every table the normal DB may contain (FTS shadow tables are the 'fts' siblings below).
TABLES = ("subjects", "publishers", "authors", "sources", "source_authors", "facts",
          "fact_revisions", "fact_sources")
FTS_TABLES = ("facts_fts", "sources_fts", "authors_fts")
VIEWS = ("v_subjects",)

FACT_COLS = ("id, subject_id, statement, is_original_claim, trust_level, trust_rationale, status, "
             "superseded_by_fact_id, date_added, last_reviewed_at, recheck_by, recheck_rationale, "
             "visibility, source_key, freshness")
# FACT_COLS positions copied as free text: r[5] trust_rationale, r[11] recheck_rationale
REVISION_COLS = ("id, fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, "
                 "trust_rationale, status, visibility, superseded_by, recheck_by, recheck_rationale, freshness")
# REVISION_COLS positions copied as free text: r[8] trust_rationale, r[13] recheck_rationale
SOURCE_COLS = "id, citekey, name, source_type, publisher_id, url, published_date, retrieved_date, description"


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
    return [r for r in rows if flt.passes(r[1], r[2], r[12], extra=(r[5], r[11], *cite_text.get(r[0], ())))]


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
        if r[1] not in fact_ids or not flt.passes(subject_of[r[1]], r[6], r[10], extra=(r[8], r[13])):
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
