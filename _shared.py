"""Small helpers shared across ingest scripts -- author/publisher/vault-file
normalization (get-or-create against a dimension table, never repeated text)."""
from fact_ingest_rules import require_date_added  # noqa: F401  (re-exported for the ingest scripts)
from source_ingest_rules import split_authors
from artist_rules import ARTIST_ALIASES, ARTIST_MEMBERS, artist_key, canonical_artist_name  # noqa: F401  (re-exported for the ingest scripts)

import acquisition_store
import fixity_store
import identifiers


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
    """The vault_files id for `path`, with its current fixity record (#38) written on first sight.
    A file that cannot be read gets file_state 'missing' and no hash; this never raises for a bad path."""
    if not path:
        return None
    file_id = get_or_create(cur, "vault_files", "path", path)
    fp = fixity_store.fingerprint(path)
    # #46: a vault file has no data record, so its custodial history comes from the macOS file attributes
    # alone (a no-op elsewhere); acquisition_store.resolve marks anything it fills in acquired_note.
    acq = acquisition_store.resolve({k: None for k in acquisition_store.COLUMNS}, path)
    cur.execute("UPDATE vault_files SET content_sha256 = :content_sha256, size_bytes = :size_bytes, "
                "file_mtime = :file_mtime, mime_type = :mime_type, file_state = :file_state, "
                "acquired_at = :acquired_at, acquired_via = :acquired_via, where_from = :where_from, "
                "acquired_note = :acquired_note WHERE id = :id",
                {**fp, **acq, "id": file_id})
    return file_id


def collect_identifiers(raw, where):
    """[(scheme, normalized value)] from a source's data; see identifiers.collect."""
    return identifiers.collect(raw, where)


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


def load_artist_cache(cur):
    """id keyed by lowercased name, for case-insensitive artist resolution
    shared across the music ingest scripts (concerts/albums/scrobbles) --
    Last.fm, RYM, and hand-typed concert names disagree on artist casing
    ("JPEGMAFIA" vs "Jpegmafia") far more often than you'd guess, and SQL's
    own NOCASE collation only folds ASCII, so the fold happens in Python."""
    cache = {}
    for id_, name in cur.execute("SELECT id, name FROM artists"):
        cache.setdefault(artist_key(name), id_)
    return cache


def get_or_create_artist(cur, cache, name):
    # (RYM's HTML-escaping and the alias table are applied by artist_rules.canonical_artist_name.)
    name = canonical_artist_name(name)
    key = artist_key(name)
    if key in cache:
        return cache[key]
    cur.execute("INSERT INTO artists (name) VALUES (?)", (name,))
    id_ = cur.lastrowid
    cache[key] = id_
    return id_


def seed_artist_members(cur, cache):
    """Populate artist_members from ARTIST_MEMBERS. Call after the ingest
    scripts have run, so the real member artists (e.g. "Madlib") already
    exist from their own solo credits rather than being created fresh here
    with no other data attached."""
    inserted = 0
    for group_name, member_names in ARTIST_MEMBERS.items():
        group_id = get_or_create_artist(cur, cache, group_name)
        for member_name in member_names:
            member_id = get_or_create_artist(cur, cache, member_name)
            cur.execute(
                "INSERT OR IGNORE INTO artist_members (artist_id, member_id) VALUES (?, ?)",
                (group_id, member_id),
            )
            inserted += cur.rowcount
    return inserted


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
