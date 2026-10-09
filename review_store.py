"""Adapter for review (#6): the reads behind the review queue. The pending rows and subject tree come
from the built db, the current states from the entry files and the revision log. The decisions are
in `review_rules`; the use case is `review`.
"""
import os

import revisions
import revisions_store
import review_rules


def default_data_dir():
    """The private data directory the revision log lives in (what `data_dir=None` means)."""
    return revisions_store.default_data_dir()


def list_pending(db):
    """Pending facts of a built db, oldest first (date_added, then id). `db` is a sqlite connection
    or a path (opened read-only). Each row: id, source_key, subject, statement, visibility,
    trust_level, captured_via, session_id, captured_at, source_quote, date_added."""
    with revisions_store.connection(db) as con:
        cur = con.execute(
            """SELECT f.id, f.source_key, s.name AS subject, f.statement, f.visibility, f.trust_level,
                      f.captured_via, f.session_id, f.captured_at, f.source_quote, f.date_added
               FROM facts f JOIN subjects s ON s.id = f.subject_id
               WHERE f.status = 'pending' ORDER BY f.date_added, f.id""")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def current_states(data_dir=None):
    """{source_key: current mutable snapshot incl. revision} from the entry files and revision
    log, in entry order (dict order). Raises revisions.RevisionError if either is unusable."""
    data_dir = default_data_dir() if data_dir is None else data_dir
    entries = revisions_store.load_entries(data_dir)
    # The file name matters: it is what marks an entry in pilot_facts.json / facts_batch*.json that
    # has no `freshness` as legacy ('unreviewed'/'recheck') instead of an error.
    states = {e["key"]: revisions.implicit_revision(e["key"], e["entry"], e["legacy_date"], e["file"])
              for e in entries}
    for _, rec in revisions_store.read_log(os.path.join(data_dir, revisions.REVISIONS_FILENAME)):
        if rec["source_key"] in states:
            states[rec["source_key"]] = rec
    return states


def subject_parents(db):
    """{subject name: parent name | None} from the db (read-only). Raises on an unreadable db."""
    with revisions_store.connection(db) as con:
        rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
    return {n: p for n, p in rows}


def source_key_for_id(db, fact_id):
    """The source_key of fact `fact_id` (None if the fact has none), or review_rules.NO_ROW if there is no such fact."""
    with revisions_store.connection(db) as con:
        row = con.execute("SELECT source_key FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return review_rules.NO_ROW if row is None else row[0]
