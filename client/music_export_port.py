"""Port for what the music-taste export reads: albums, concerts and per-artist rollups as typed records. The
adapter (`music_export_store`) owns the database and its SQL; the export builds its document from these
records and never sees a cursor or a connection."""
from typing import List, Optional, Protocol, Tuple, TypedDict


class AlbumRecord(TypedDict):
    artist: str
    title: str
    release_year: Optional[int]
    rating: Optional[float]
    rym_id: Optional[str]


class ConcertRecord(TypedDict):
    artist: str
    start_date: Optional[str]
    end_date: Optional[str]
    venue: Optional[str]
    city_state: Optional[str]
    festival: Optional[str]
    billing: Optional[str]
    supporting_for: Optional[str]


class ArtistRecord(TypedDict):
    artist: str
    albums_rated: int
    avg_album_rating: Optional[float]
    concerts_seen: int


class MusicExportReads(Protocol):
    def albums(self) -> List[AlbumRecord]: ...
    def concerts(self) -> List[ConcertRecord]: ...
    def artists(self) -> List[ArtistRecord]: ...
    def album_totals(self) -> Tuple[int, Optional[float]]: ...
    def close(self) -> None: ...
