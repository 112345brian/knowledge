"""The artists of one import run, keyed the one way the build compares artist names.

Domain service ring: it asks the build's `MusicRepository` for the artists already stored and for a new one to be
added, and decides nothing else. Artist identity is defined here and in `client.artist_rules` (the key and the
canonical spelling) and nowhere else; `identity_service.ArtistIdentity` is this class plus the group memberships, and
every music step goes through it.
"""
from typing import Dict, Mapping, Optional

import build_records as r
from build_repositories_port import MusicRepository
from client import artist_rules


class ArtistDirectory:
    """Artist ids for the names of one run, keyed case-insensitively, created in first-seen order.

    The artists already in the database are read at construction (the first id wins when two differ only by case);
    `resolve` returns the id for a source name, adding the artist (under its canonical spelling) when it is new."""

    def __init__(self, music: MusicRepository, aliases: Optional[Mapping[str, str]] = None):
        self._music = music
        self._aliases = dict(aliases or {})
        self._ids: Dict[str, r.ArtistId] = {}
        for artist_id, name in music.artists():
            self._ids.setdefault(artist_rules.artist_key(name), artist_id)

    def resolve(self, name: str) -> r.ArtistId:
        name = artist_rules.canonical_artist_name(name, self._aliases)
        key = artist_rules.artist_key(name)
        if key not in self._ids:
            self._ids[key] = self._music.add_artist(name)
        return self._ids[key]

    def __len__(self) -> int:
        """Distinct artist keys known so far (the scrobbles summary reports the growth)."""
        return len(self._ids)
