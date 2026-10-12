"""The artist directory plus the configured group memberships, over the record-shaped music contract.

Client-owned (it used to be `identity_service.ArtistIdentity`): artist identity is music-source logic. Every music
step resolves names through it (`music_service`). Application ring.
"""
from client import music_records as m
from client.artist_directory import ArtistDirectory
from client.music_port import MusicRepository


class ArtistIdentity(ArtistDirectory):
    """The artist directory plus the configured group memberships."""
    def __init__(self, music: MusicRepository, rules: m.ArtistRules):
        super().__init__(music, rules.aliases)
        self._rules = rules

    def seed_members(self) -> int:
        """Record the configured group memberships; returns how many rows were new. Run after the imports so the
        members already exist from their own credits."""
        inserted = 0
        for group_name, member_names in self._rules.members.items():
            group_id = self.resolve(group_name)
            for member_name in member_names:
                if self._music.add_artist_member(group_id, self.resolve(member_name)):
                    inserted += 1
        return inserted
