"""Pure helpers for canonicalizing names from optional music sources, and the shape of artist_rules.json.

The two name functions are defined once, in `identity_rules` (the one place artist identity is defined); this module
re-exports them for the music steps and keeps the artist rules file's own shape.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

from identity_rules import artist_key, canonical_artist_name  # noqa: F401  (re-exported)


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
