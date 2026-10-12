"""Pure helpers for canonicalizing names from optional music sources, and the reading of artist_rules.json.

The two name functions are music-source logic (RYM HTML-escapes names; the aliases come from artist_rules.json), so
they are defined here, once. `artist_directory` is the stateful user; `identity_service` and `music_ingest_service`
reach them through it. The rules themselves are `build_records.ArtistRules`, the only definition of that record;
`parse_artist_rules` is the only place that builds one from the file's content.
"""
import html
from typing import Any, Mapping, Optional

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


def parse_artist_rules(data: Any, path: str) -> r.ArtistRules:
    """`ArtistRules` from the parsed artist_rules.json (`path` only names the file in the error); RuntimeError when
    it is not version 1 with an aliases object, a members object of lists and (optionally) a festivals list."""
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("aliases"), dict)
            or not isinstance(data.get("members"), dict)
            or not all(isinstance(m, list) for m in data["members"].values())
            or not isinstance(data.get("festivals", []), list)):
        raise RuntimeError(f"{path}: expected version 1 with aliases and members")
    return r.ArtistRules(dict(data["aliases"]), {g: tuple(m) for g, m in data["members"].items()},
                         tuple(data.get("festivals", ())))
