"""The plain values of the music source's record-shaped repository contract (`music_port.MusicRepository`).

Client-owned: these used to sit in the core's `build_records`, which made the core's contract name a client source.
Fields and meaning are unchanged; only the owner moved. A client module may import core types; the core never
imports this module.

Domain service ring: frozen dataclasses and identifier types, no I/O.
"""
from dataclasses import dataclass, field
from typing import Mapping, NewType, Optional, Tuple

ArtistId = NewType("ArtistId", int)
ImportSourceId = NewType("ImportSourceId", int)
VenueId = NewType("VenueId", int)
FestivalId = NewType("FestivalId", int)
TrackId = NewType("TrackId", int)


@dataclass(frozen=True)
class NewAttendance:
    artist_id: ArtistId
    start_date: str
    end_date: Optional[str]
    venue_id: Optional[VenueId]
    festival_id: Optional[FestivalId]
    billing: Optional[str]
    supporting_for_artist_id: Optional[ArtistId]
    notes: Optional[str]
    import_source_id: ImportSourceId
    domain: str = "music"


@dataclass(frozen=True)
class NewAlbum:
    artist_id: ArtistId
    title: str
    release_year: Optional[int]
    rating: Optional[float]
    rym_id: Optional[str]
    import_source_id: ImportSourceId


@dataclass(frozen=True)
class NewScrobble:
    track_id: TrackId
    played_at: object
    import_source_id: ImportSourceId


@dataclass(frozen=True)
class MusicTotals:
    """The table sizes the music steps report (any may be unused by a given step)."""
    artists: int
    venues: int
    festivals: int
    tracks: int
    scrobbles: int


@dataclass(frozen=True)
class ArtistRules:
    """artist_rules.json: aliases (lowercased source name -> canonical name), group memberships, known festivals.
    The one definition: `client.artist_rules.parse_artist_rules` builds it; the default is no rules."""
    aliases: Mapping[str, str] = field(default_factory=dict)
    members: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    festivals: Tuple[str, ...] = ()
