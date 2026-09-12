"""Small helpers shared across ingest scripts -- author/publisher/vault-file
normalization (get-or-create against a dimension table, never repeated text)."""


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


def get_or_create_vault_file(cur, path):
    if not path:
        return None
    return get_or_create(cur, "vault_files", "path", path)


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
