"""The artist directory plus the configured group memberships, over the record-shaped music contract.

Client-owned (it used to be `identity_service.ArtistIdentity`): artist identity is music-source logic. No production
step uses it (`music_service.seed_artist_members` does the same with `client.artist_rules.ArtistRules`); it is kept
as it was, with its tests, for the music collapse to merge. Application ring.
"""
from client import music_records as m
from client.artist_directory import ArtistDirectory
from client.music_port import MusicRecordRepository


class ArtistIdentity(ArtistDirectory):
    """The artist directory plus the configured group memberships."""
    def __init__(self, music: MusicRecordRepository, rules: m.ArtistRules):
        super().__init__(music, rules.aliases)
        self._music = music
        self._rules = rules

    def seed_members(self) -> int:
        """Record the configured group memberships; returns how many rows were new. Run after the imports so the
        members already exist from their own credits."""
        inserted = 0
        for group_name, member_names in self._rules.members.items():
            group_id = m.ArtistId(self.resolve(group_name))
            for member_name in member_names:
                if self._music.add_artist_member(group_id, m.ArtistId(self.resolve(member_name))):
                    inserted += 1
        return inserted
