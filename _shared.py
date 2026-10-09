"""Small helpers shared across ingest scripts -- author/publisher/vault-file
normalization (get-or-create against a dimension table, never repeated text)."""
import html

import acquisition
import fixity


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
    fp = fixity.fingerprint(path)
    # #46: a vault file has no data record, so its custodial history comes from the macOS file attributes
    # alone (a no-op elsewhere); acquisition.resolve marks anything it fills in acquired_note.
    acq = acquisition.resolve({k: None for k in acquisition.COLUMNS}, path)
    cur.execute("UPDATE vault_files SET content_sha256 = :content_sha256, size_bytes = :size_bytes, "
                "file_mtime = :file_mtime, mime_type = :mime_type, file_state = :file_state, "
                "acquired_at = :acquired_at, acquired_via = :acquired_via, where_from = :where_from, "
                "acquired_note = :acquired_note WHERE id = :id",
                {**fp, **acq, "id": file_id})
    return file_id


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
        cache.setdefault(name.lower(), id_)
    return cache


# Same-artist spelling variants that case-folding alone doesn't catch --
# punctuation/spacing/stylization differences across RYM, Last.fm, and
# hand-typed concert names ("TR/ST" vs "TRST", "Bri" vs "Brian Powers").
# Found by normalizing every artists.name to lower+alnum-only and grouping.
# A joint credit like "Freddie Gibbs & Madlib" stays its OWN artist row --
# see ARTIST_MEMBERS below for how its real members get recorded without
# decomposing every track/album it's credited on.
# Keys are lowercased; values are the canonical name to store instead.
ARTIST_ALIASES = {
    "ahn dayoung": "Ahn Da-young",
    "black eyed peas": "The Black Eyed Peas",
    "body": "The Body",
    "brave little abacus": "The Brave Little Abacus",
    "chaoschaos": "Chaos Chaos",
    "combatwoundedveteran": "Combat Wounded Veteran",
    "the destroyer": "Destroyer",
    "e l u c i d": "Elucid",
    "フィッシュマンズ [fishmans]": "Fishmans",
    "freddie gibbs, madlib": "Freddie Gibbs & Madlib",
    "harunemuri": "Haru Nemuri",
    "jay z": "JAY-Z",
    "j.i.d": "JID",
    "lil' wayne": "Lil Wayne",
    "(liv).e": "Liv.e",
    "locust": "The Locust",
    "l’rain": "L'Rain",
    "マクロスmacross 82-99": "Macross 82-99",
    "microphones": "The Microphones",
    "the misfits": "Misfits",
    "n*e*r*d": "N.E.R.D",
    "parrygripp": "Parry Gripp",
    "the peace": "Peace",
    "rah band": "The Rah Band",
    "the ramones": "Ramones",
    "ratboy": "RAT BOY",
    "seeyouspacecowboy...": "SeeYouSpaceCowboy",
    "smashing pumpkins": "The Smashing Pumpkins",
    "the spirit of the beehive": "Spirit of the Beehive",
    "spiritualized®": "Spiritualized",
    "スティーブ・ハイェット [steve hiett]": "Steve Hiett",
    "落日飛車 sunset rollercoaster": "Sunset Rollercoaster",
    "three-6 mafia": "Three 6 Mafia",
    "three 6 mafia": "Three 6 Mafia",
    "t. p. orchestre poly-rythmo": "T.P. Orchestre Poly-Rythmo",
    "trst": "TR/ST",
    "tyler  the creator": "Tyler, The Creator",
    "tyler the creator": "Tyler, The Creator",
    "x-marks the pedwalk": "X Marks the Pedwalk",
    "bri": "Brian Powers",
}


def get_or_create_artist(cur, cache, name):
    # RYM's export HTML-escapes special characters (stored literally as
    # "Gibbs &amp; Madlib") and never gets decoded before this -- same class
    # of bug the sibling rave-recommender project already found and fixed
    # in its own v1->v2 migration ("Fred again.. &amp; Skrillex").
    name = html.unescape(name)
    name = ARTIST_ALIASES.get(name.lower(), name)
    key = name.lower()
    if key in cache:
        return cache[key]
    cur.execute("INSERT INTO artists (name) VALUES (?)", (name,))
    id_ = cur.lastrowid
    cache[key] = id_
    return id_


# Group/collab credits and their real members -- an artist-to-artist fact,
# not tied to any specific track/album (see the artist_members comment in
# schema.sql for why decomposing per-credit was tried and reverted). Curated
# by hand, same spirit as ARTIST_ALIASES: add an entry when you notice one.
ARTIST_MEMBERS = {
    "Freddie Gibbs & Madlib": ["Freddie Gibbs", "Madlib"],
    "Madvillain": ["Madlib", "MF DOOM"],
    "We Needed This and Brian Powers": ["We Needed This", "Brian Powers"],
}


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


def require_date_added(item, filename, index):
    """The entry's own `date_added`, validated. A missing, null, blank or non-ISO value is a
    build error naming the file and entry -- never a made-up date (#35). Fix the data, or run
    backfill_dates.py for the original entries."""
    from datetime import datetime
    value = item.get("date_added")
    try:
        if not isinstance(value, str):
            raise ValueError
        datetime.fromisoformat(value)
    except ValueError:
        what = "has no `date_added`" if "date_added" not in item else f"has an invalid `date_added` {value!r}"
        raise ValueError(
            f"{filename}[{index}] ({str(item.get('statement') or '')[:60]!r}) {what}. A date is never invented: "
            f"set it by hand, or for the original entries run `python3 backfill_dates.py --apply`.") from None
    return value
