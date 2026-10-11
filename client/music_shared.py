"""Helpers shared by optional music ingesters; source-specific identity rules live in private data."""
import json
import os

from client.artist_rules import artist_key, canonical_artist_name


def load_artist_rules(inputs, data_dir):
    """Load optional aliases and group memberships from artist_rules.json in `data_dir`."""
    path = os.path.join(data_dir, "artist_rules.json")
    if not inputs.exists(path):
        return {"aliases": {}, "members": {}, "festivals": []}
    try:
        rules = inputs.read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"{path}: cannot read artist rules: {e}") from e
    if (not isinstance(rules, dict) or rules.get("version") != 1
            or not isinstance(rules.get("aliases"), dict)
            or not isinstance(rules.get("members"), dict)
            or not isinstance(rules.get("festivals", []), list)):
        raise RuntimeError(f"{path}: expected version 1 with aliases and members")
    return rules


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


def get_or_create_artist(cur, cache, name, rules):
    # (RYM's HTML-escaping and the alias table are applied by artist_rules.canonical_artist_name.)
    name = canonical_artist_name(name, rules["aliases"])
    key = artist_key(name)
    if key in cache:
        return cache[key]
    cur.execute("INSERT INTO artists (name) VALUES (?)", (name,))
    id_ = cur.lastrowid
    cache[key] = id_
    return id_


def seed_artist_members(cur, cache, rules):
    """Populate artist_members from private configuration. Call after the ingest
    scripts have run, so the member artists already
    exist from their own solo credits rather than being created fresh here
    with no other data attached."""
    inserted = 0
    for group_name, member_names in rules["members"].items():
        group_id = get_or_create_artist(cur, cache, group_name, rules)
        for member_name in member_names:
            member_id = get_or_create_artist(cur, cache, member_name, rules)
            cur.execute(
                "INSERT OR IGNORE INTO artist_members (artist_id, member_id) VALUES (?, ?)",
                (group_id, member_id),
            )
            inserted += cur.rowcount
    return inserted
