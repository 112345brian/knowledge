"""Small helpers shared across the ingest scripts: the fact hints file and the generic get-or-create against a
dimension table. Publishers, authors, identifiers and vault files are the source repository's (`sources_port`)."""
import os

import fact_ingest_service
from ingest import fact_ingest_rules
from ingest.fact_ingest_rules import require_date_added  # noqa: F401  (re-exported for the ingest scripts)


HINTS_FILENAME = fact_ingest_service.HINTS_FILENAME


def load_hints(inputs, data_dir):
    """The author's fact-ingest hints from fact_hints.json in the private data dir (see fact_ingest_rules.Hints), or
    NO_HINTS when the file is absent. A file that is not valid JSON or not a valid hints object fails the build."""
    return fact_ingest_service.read_hints(inputs, os.path.join(data_dir, HINTS_FILENAME))


def get_or_create(cur, table, name_col, name):
    row = cur.execute(f"SELECT id FROM {table} WHERE {name_col} = ?", (name,)).fetchone()
    if row:
        return row[0]
    cur.execute(f"INSERT INTO {table} ({name_col}) VALUES (?)", (name,))
    return cur.lastrowid
