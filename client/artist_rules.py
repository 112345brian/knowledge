"""Pure helpers for canonicalizing names from optional music sources, and the shape of artist_rules.json.

The two name functions are music-source logic (RYM HTML-escapes names; the aliases come from artist_rules.json), so
they are defined here, once. `artist_directory` is the stateful user; `artist_identity_service` and `music_service` reach
them through it.
"""
import html
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from client import music_records


def canonical_artist_name(name: str, aliases: Optional[Mapping[str, str]] = None) -> str:
    """Unescape a source name (RYM HTML-escapes), then apply a caller-provided alias mapping keyed by the
    lowercased unescaped name."""
    name = html.unescape(name)
    return (aliases or {}).get(name.lower(), name)


def artist_key(name: str) -> str:
    """Case-insensitive cache key. Python's `lower()`, not SQLite NOCASE (which folds ASCII only): Last.fm, RYM and
    hand-typed concert names disagree on casing ("JPEGMAFIA" vs "Jpegmafia") far more than on spelling."""
    return name.lower()


@dataclass(frozen=True)
class ArtistRules:
    """The optional private aliases, group memberships and known festival names. The default is no rules."""
    aliases: Dict[str, str] = field(default_factory=dict)
    members: Dict[str, List[str]] = field(default_factory=dict)
    festivals: List[str] = field(default_factory=list)


def parse_artist_rules(data: Any, path: str) -> ArtistRules:
    """`ArtistRules` from the parsed artist_rules.json (`path` only names the file in the error); RuntimeError when
    it is not version 1 with an aliases object, a members object and (optionally) a festivals list."""
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("aliases"), dict)
            or not isinstance(data.get("members"), dict)
            or not isinstance(data.get("festivals", []), list)):
        raise RuntimeError(f"{path}: expected version 1 with aliases and members")
    return ArtistRules(aliases=data["aliases"], members=data["members"], festivals=data.get("festivals", []))


def artist_rules_from_mapping(raw: Mapping) -> music_records.ArtistRules:
    """`artist_rules.json`'s decoded, already validated content as the record (see music_shared.load_artist_rules)."""
    return music_records.ArtistRules(dict(raw["aliases"]), {g: tuple(m) for g, m in raw["members"].items()},
                                     tuple(raw.get("festivals", ())))
