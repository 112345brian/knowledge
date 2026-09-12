"""Small helpers shared across ingest scripts -- author normalization."""


def get_or_create_author(cur, name):
    row = cur.execute("SELECT id FROM authors WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO authors (name) VALUES (?)", (name,))
    return cur.lastrowid


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
