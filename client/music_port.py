"""Port for the music tables the client music steps write: artists, venues, festivals, attendances, albums, tracks,
scrobbles and the import sources they came from. Client-owned: the core's build contract does not know them.

One contract, record-shaped (`music_records`): the services map a CSV row or a scrobble to the records the repository
stores, decide which rows a rerun replaces and commit through the build's unit of work; the adapter (`music_store`)
owns every statement of SQL and decides nothing.

One port rather than one per step because the three importers share one artist identity (an artist seen live, rated
on RYM and scrobbled is a single row); the service resolves names to ids through it.

`import_source`, `venue` and `festival` are get-or-create: the same arguments give the same id. `add_artist` is
only called for a name `artists()` did not already list (the service keeps the case-folded cache).
"""
from typing import Iterable, Optional, Protocol, Sequence, Tuple

from client import music_records as m


class MusicRepository(Protocol):
    def import_source(self, path: Optional[str]) -> m.ImportSourceId:
        """Get-or-create the import_sources row for an input file."""
    def delete_attendances(self, import_source_id: m.ImportSourceId) -> None: ...
    def delete_albums(self, import_source_id: m.ImportSourceId) -> None: ...
    def delete_scrobbles(self, import_source_id: m.ImportSourceId) -> None: ...
    def artists(self) -> Iterable[Tuple[m.ArtistId, str]]:
        """(id, name) of every artist, in table order."""
    def add_artist(self, name: str) -> m.ArtistId: ...
    def venue(self, name: str, city_state: Optional[str]) -> m.VenueId:
        """Get-or-create on (name, city_state); NULL matches NULL."""
    def festival(self, name: str) -> m.FestivalId:
        """Get-or-create."""
    def add_attendance(self, attendance: m.NewAttendance) -> None: ...
    def add_album(self, album: m.NewAlbum) -> None: ...
    def tracks(self) -> Iterable[Tuple[m.TrackId, m.ArtistId, str, Optional[str]]]:
        """(id, artist id, title, album) of every track."""
    def add_track(self, artist_id: m.ArtistId, title: str, album: Optional[str]) -> m.TrackId: ...
    def add_scrobbles(self, scrobbles: Sequence[m.NewScrobble]) -> None:
        """INSERT OR IGNORE, in the order given."""
    def add_artist_member(self, group_id: m.ArtistId, member_id: m.ArtistId) -> bool:
        """INSERT OR IGNORE; True when a row was added."""
    def totals(self) -> m.MusicTotals: ...
