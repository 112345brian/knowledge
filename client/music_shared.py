"""Helpers shared by the music source's steps: artist identity (case-insensitive, aliased) and the curated group
memberships. Needs the `music` schema fragment (artists, artist_members)."""
from client.artist_rules import ARTIST_ALIASES, ARTIST_MEMBERS, artist_key, canonical_artist_name  # noqa: F401


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
