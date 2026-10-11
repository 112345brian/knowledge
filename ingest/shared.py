"""Small helpers shared across the core ingest scripts -- author/publisher
normalization (get-or-create against a dimension table, never repeated text). A vault file's fingerprint is the fact
repository's (fact_ingest_service.ensure_vault_file)."""
import fact_ingest_service
from ingest.source_ingest_rules import split_authors

import identifiers


HINTS_FILENAME = fact_ingest_service.HINTS_FILENAME


def load_hints(inputs, data_dir):
    """The author's fact-ingest hints from fact_hints.json in the private data dir (see fact_ingest_rules.Hints), or
    NO_HINTS when the file is absent. A file that is not valid JSON or not a valid hints object fails the build."""
    return fact_ingest_service.read_hints(inputs, data_dir)


def get_or_create(cur, table, name_col, name):
    row = cur.execute(f"SELECT id FROM {table} WHERE {name_col} = ?", (name,)).fetchone()
    if row:
        return row[0]
    cur.execute(f"INSERT INTO {table} ({name_col}) VALUES (?)", (name,))
    return cur.lastrowid


def get_or_create_publisher(cur, name):
    if not name:
        return None
    return get_or_create(cur, "publishers", "name", name)


def collect_identifiers(raw, where):
    """[(scheme, normalized value)] from a source's data; see identifiers.collect. A malformed identifier is
    reported as a warning naming `where` and is not recorded; it does not stop the build or the source's other
    identifiers (a typo in one DOI is a data problem to fix, not a reason to have no knowledge.db)."""
    problems = []
    pairs = identifiers.collect(raw, where, problems)
    for message in problems:
        print(f"  WARNING -- {message}; identifier not recorded")
    return pairs


def add_source_identifiers(cur, source_id, citekey, pairs):
    """Insert (scheme, value) pairs for a source. A pair already owned by ANOTHER source is not inserted; it goes to
    source_identifier_conflicts and is reported (a warning line naming both citekeys); nothing is merged.
    Returns the list of conflicts [(scheme, value, owner citekey)]."""
    conflicts = []
    for scheme, value in pairs:
        row = cur.execute("SELECT si.source_id, s.citekey FROM source_identifiers si JOIN sources s ON s.id = si.source_id "
                          "WHERE si.scheme = ? AND si.value = ?", (scheme, value)).fetchone()
        if row is None:
            cur.execute("INSERT INTO source_identifiers (source_id, scheme, value) VALUES (?, ?, ?)", (source_id, scheme, value))
        elif row[0] != source_id:
            cur.execute("INSERT OR IGNORE INTO source_identifier_conflicts (source_id, scheme, value, owner_source_id) VALUES (?, ?, ?, ?)",
                        (source_id, scheme, value, row[0]))
            conflicts.append((scheme, value, row[1]))
            print(f"  WARNING -- duplicate identifier {scheme}:{value} shared by sources {row[1]!r} and {citekey!r} (not merged)")
    return conflicts


def get_or_create_author(cur, name):
    return get_or_create(cur, "authors", "name", name)


def link_authors(cur, source_id, author_string):
    """Split a '; '-joined author string and link each to source_authors,
    preserving byline order. No-op if author_string is falsy."""
    if not author_string:
        return
    for i, name in enumerate(a.strip() for a in author_string.split(";")):
        if not name:
            continue
        author_id = get_or_create_author(cur, name)
        cur.execute(
            "INSERT OR IGNORE INTO source_authors (source_id, author_id, author_order) VALUES (?, ?, ?)",
            (source_id, author_id, i),
        )
