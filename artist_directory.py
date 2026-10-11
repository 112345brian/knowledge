"""The artists of one import run, keyed the one way the build compares artist names.

Domain service ring: it asks a repository for the artists already stored and for a new one to be added, and decides
nothing else. Artist identity is defined here and in `identity_rules` (the key and the canonical spelling) and nowhere
else; `identity_service.ArtistIdentity` and `music_ingest_service` both use this class rather than keep a copy.
"""
from typing import Dict, Iterable, Mapping, Optional, Protocol, Tuple

import identity_rules


class ArtistStore(Protocol):
    def artists(self) -> Iterable[Tuple[int, str]]: ...
    def add_artist(self, name: str) -> int: ...


class ArtistDirectory:
    """Artist ids for the names of one run, keyed case-insensitively, created in first-seen order.

    The artists already in the database are read at construction (the first id wins when two differ only by case);
    `resolve` returns the id for a source name, adding the artist (under its canonical spelling) when it is new."""

    def __init__(self, store: ArtistStore, aliases: Optional[Mapping[str, str]] = None):
        self._store = store
        self._aliases = dict(aliases or {})
        self._ids: Dict[str, int] = {}
        for artist_id, name in store.artists():
            self._ids.setdefault(identity_rules.artist_key(name), artist_id)

    def resolve(self, name: str) -> int:
        name = identity_rules.canonical_artist_name(name, self._aliases)
        key = identity_rules.artist_key(name)
        if key not in self._ids:
            self._ids[key] = self._store.add_artist(name)
        return self._ids[key]

    def __len__(self) -> int:
        """Distinct artist keys known so far (the scrobbles summary reports the growth)."""
        return len(self._ids)
