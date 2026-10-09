"""Artist identity rules for the music ingests (domain: no db, no files).

How an artist name from RYM, Last.fm or a hand-typed concert list is canonicalized: HTML-unescaped, then
mapped through the curated alias table, then compared case-insensitively. The curated membership table of
group credits lives here too. `shared` (the adapter) does the get-or-create against the db.
"""
import html

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


def canonical_artist_name(name):
    """The name to store: RYM's export HTML-escapes special characters (stored literally as "Gibbs &amp; Madlib")
    and never gets decoded before this, then the curated aliases fold spelling variants onto one spelling."""
    name = html.unescape(name)
    return ARTIST_ALIASES.get(name.lower(), name)


def artist_key(name):
    """The case-insensitive cache key of a canonical name (SQL's NOCASE only folds ASCII)."""
    return name.lower()


# Group/collab credits and their real members -- an artist-to-artist fact,
# not tied to any specific track/album (see the artist_members comment in
# schema.sql for why decomposing per-credit was tried and reverted). Curated
# by hand, same spirit as ARTIST_ALIASES: add an entry when you notice one.
ARTIST_MEMBERS = {
    "Freddie Gibbs & Madlib": ["Freddie Gibbs", "Madlib"],
    "Madvillain": ["Madlib", "MF DOOM"],
    "We Needed This and Brian Powers": ["We Needed This", "Brian Powers"],
}
