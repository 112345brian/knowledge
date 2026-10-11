"""Identity rules the build shares, pure: how names and keys are normalized before they are compared.

Domain service ring: no I/O. This is the only place the artist-name canonicalization and the byline split are
defined (`client/artist_rules.py` re-exports the artist ones for the music steps that still import it). The
stateful operations that use them (find or create, with a per-run cache) are `identity_service`.
"""
import html
from typing import List, Mapping, Optional, Tuple

import build_records as r


def canonical_artist_name(name: str, aliases: Optional[Mapping[str, str]] = None) -> str:
    """Unescape a source name (RYM HTML-escapes), then apply a caller-provided alias mapping keyed by the
    lowercased unescaped name."""
    name = html.unescape(name)
    return (aliases or {}).get(name.lower(), name)


def artist_key(name: str) -> str:
    """Case-insensitive cache key. Python's `lower()`, not SQLite NOCASE (which folds ASCII only): Last.fm, RYM and
    hand-typed concert names disagree on casing ("JPEGMAFIA" vs "Jpegmafia") far more than on spelling."""
    return name.lower()


def artist_rules_from_mapping(raw: Mapping) -> r.ArtistRules:
    """`artist_rules.json`'s decoded, already validated content as the record (see music_shared.load_artist_rules)."""
    return r.ArtistRules(dict(raw["aliases"]), {g: tuple(m) for g, m in raw["members"].items()},
                         tuple(raw.get("festivals", ())))


def author_links(author_string: Optional[str]) -> List[Tuple[str, int]]:
    """[(author name, byline position)] for a '; '-joined byline. Names are stripped and blanks skipped, but a
    blank still occupies a position, so `"A; ; B"` gives [("A", 0), ("B", 2)]. Falsy input gives []."""
    if not author_string:
        return []
    return [(name, i) for i, name in enumerate(a.strip() for a in author_string.split(";")) if name]


def identifier_conflict_warning(scheme: str, value: str, owner_citekey: Optional[str], citekey: Optional[str]) -> str:
    """The build's one line about an identifier two sources claim."""
    return (f"  WARNING -- duplicate identifier {scheme}:{value} shared by sources {owner_citekey!r} "
            f"and {citekey!r} (not merged)")
