"""Pure helpers for canonicalizing names from optional music sources."""
import html


def canonical_artist_name(name, aliases=None):
    """Unescape a source name and apply a caller-provided alias mapping."""
    name = html.unescape(name)
    return (aliases or {}).get(name.lower(), name)


def artist_key(name):
    """Case-insensitive cache key (SQLite's NOCASE only folds ASCII)."""
    return name.lower()
