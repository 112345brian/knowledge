"""In-memory stand-ins for the CLIENT sources' record-shaped repositories (music, measurements, claims): the fakes
that used to sit in `fake_build_repositories` beside the core's, when the core's `BuildSession` carried them.

`FakeClientState` is a `FakeState` plus the client tables, so one state still serves a core fake and a client fake
in the same test. The core's fakes know nothing here.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

import build_records as r
from client import claims_port as cp
from client import measurements_records as mr
from client import music_records as mu
from fake_build_repositories import FakeState, _next


@dataclass
class FakeClientState(FakeState):
    fact_measurements: List[Tuple[int, str]] = field(default_factory=list)
    # measurements
    metrics: Dict[int, mr.NewMetric] = field(default_factory=dict)
    measurements: List[mr.NewMeasurement] = field(default_factory=list)
    exercises: Dict[str, int] = field(default_factory=dict)
    foods: Dict[str, int] = field(default_factory=dict)
    muscles: Dict[str, int] = field(default_factory=dict)
    training_sets: List[mr.NewTrainingSet] = field(default_factory=list)
    food_log: List[mr.NewFoodLogEntry] = field(default_factory=list)
    meal_log: List[mr.NewMealLogEntry] = field(default_factory=list)
    muscle_volume: List[mr.NewMuscleVolume] = field(default_factory=list)
    # claims
    claims: Dict[int, cp.NewClaim] = field(default_factory=dict)
    claim_facts: List[Tuple[int, str, str, Optional[str]]] = field(default_factory=list)
    # music
    import_sources: Dict[Optional[str], int] = field(default_factory=dict)
    artists: Dict[int, str] = field(default_factory=dict)
    venues: Dict[Tuple[str, Optional[str]], int] = field(default_factory=dict)
    festivals: Dict[str, int] = field(default_factory=dict)
    attendances: List[mu.NewAttendance] = field(default_factory=list)
    albums: List[mu.NewAlbum] = field(default_factory=list)
    tracks: Dict[int, Tuple[int, str, Optional[str]]] = field(default_factory=dict)
    scrobbles: List[mu.NewScrobble] = field(default_factory=list)
    artist_members: List[Tuple[int, int]] = field(default_factory=list)


class FakeFactLinks:
    """`measurements_service.FactLinks`: the two members the link step calls, over the state's facts and measurements."""
    def __init__(self, state: FakeClientState):
        self._s = state

    def find_by_source_key(self, source_key):
        for i, f in self._s.facts.items():
            if f.source_key == source_key:
                return r.FactId(i)
        return None

    def link_measurements(self, fact_id, metric_key):
        metric_ids = {i for i, m in self._s.metrics.items() if m.key == metric_key}
        n = sum(1 for m in self._s.measurements if m.metric_id in metric_ids)
        self._s.fact_measurements.extend([(fact_id, metric_key)] * n)
        self._s.log("facts.link_measurements", str(fact_id), metric_key)
        return n


class FakeMeasurements:
    def __init__(self, state: FakeClientState):
        self._s = state

    def find_metric(self, key):
        for i, m in self._s.metrics.items():
            if m.key == key:
                return mr.MetricId(i)
        return None

    def add_metric(self, metric):
        mid = mr.MetricId(_next(self._s.metrics))
        self._s.metrics[mid] = metric
        self._s.log("measurements.add_metric", metric.key)
        return mid

    def add(self, measurement):
        self._s.measurements.append(measurement)
        self._s.log("measurements.add")

    def _dimension(self, table: Dict[str, int], kind: str, name: str) -> int:
        if name not in table:
            table[name] = len(table) + 1
            self._s.log(f"measurements.{kind}", name)
        return table[name]

    def exercise(self, name):
        return self._dimension(self._s.exercises, "exercise", name)

    def food(self, name):
        return self._dimension(self._s.foods, "food", name)

    def muscle(self, name):
        return self._dimension(self._s.muscles, "muscle", name)

    def add_training_set(self, training_set):
        self._s.training_sets.append(training_set)
        self._s.log("measurements.add_training_set")

    def add_food_log_entry(self, entry):
        self._s.food_log.append(entry)
        self._s.log("measurements.add_food_log_entry")

    def add_meal_log_entry(self, entry):
        self._s.meal_log.append(entry)
        self._s.log("measurements.add_meal_log_entry")

    def add_muscle_volume(self, volume):
        self._s.muscle_volume.append(volume)
        self._s.log("measurements.add_muscle_volume")

    def totals(self):
        s = self._s
        return mr.MeasurementTotals(len(s.measurements), len(s.training_sets), len(s.food_log), len(s.meal_log),
                                   len(s.muscle_volume))


class FakeClaims:
    def __init__(self, state: FakeClientState):
        self._s = state

    def add(self, claim):
        cid = cp.ClaimId(_next(self._s.claims))
        self._s.claims[cid] = claim
        self._s.log("claims.add")
        return cid

    def link_facts_by_prefix(self, claim_id, prefix, role, note):
        # LIKE 'prefix%' folds ASCII case; the wildcard characters are not modelled (see the module docstring)
        hits = [i for i, f in self._s.facts.items() if f.statement.lower().startswith(prefix.lower())]
        for fid in hits:
            self._s.claim_facts.append((claim_id, f"{fid}", role, note))
        self._s.log("claims.link_facts_by_prefix", prefix)
        return len(hits)


class FakeMusic:
    def __init__(self, state: FakeClientState):
        self._s = state

    def import_source(self, path):
        s = self._s
        if path not in s.import_sources:
            s.import_sources[path] = len(s.import_sources) + 1
        return mu.ImportSourceId(s.import_sources[path])

    def delete_attendances(self, import_source_id):
        self._s.attendances = [a for a in self._s.attendances if a.import_source_id != import_source_id]
        self._s.log("music.delete_attendances")

    def delete_albums(self, import_source_id):
        self._s.albums = [a for a in self._s.albums if a.import_source_id != import_source_id]
        self._s.log("music.delete_albums")

    def delete_scrobbles(self, import_source_id):
        self._s.scrobbles = [a for a in self._s.scrobbles if a.import_source_id != import_source_id]
        self._s.log("music.delete_scrobbles")

    def artists(self):
        return [(mu.ArtistId(i), n) for i, n in self._s.artists.items()]

    def add_artist(self, name):
        aid = mu.ArtistId(_next(self._s.artists))
        self._s.artists[aid] = name
        self._s.log("music.add_artist", name)
        return aid

    def venue(self, name, city_state):
        key = (name, city_state)
        if key not in self._s.venues:
            self._s.venues[key] = len(self._s.venues) + 1
        return mu.VenueId(self._s.venues[key])

    def festival(self, name):
        if name not in self._s.festivals:
            self._s.festivals[name] = len(self._s.festivals) + 1
        return mu.FestivalId(self._s.festivals[name])

    def add_attendance(self, attendance):
        self._s.attendances.append(attendance)
        self._s.log("music.add_attendance")

    def add_album(self, album):
        self._s.albums.append(album)
        self._s.log("music.add_album")

    def tracks(self):
        return [(mu.TrackId(i), mu.ArtistId(a), t, al) for i, (a, t, al) in self._s.tracks.items()]

    def add_track(self, artist_id, title, album):
        tid = mu.TrackId(_next(self._s.tracks))
        self._s.tracks[tid] = (artist_id, title, album)
        self._s.log("music.add_track", title)
        return tid

    def add_scrobbles(self, scrobbles):
        seen = {(x.track_id, x.played_at, x.import_source_id) for x in self._s.scrobbles}
        for x in scrobbles:     # INSERT OR IGNORE against the table's uniqueness on (track, played_at, source)
            key = (x.track_id, x.played_at, x.import_source_id)
            if key not in seen:
                seen.add(key)
                self._s.scrobbles.append(x)
        self._s.log("music.add_scrobbles", str(len(scrobbles)))

    def add_artist_member(self, group_id, member_id):
        if (group_id, member_id) in self._s.artist_members:
            return False
        self._s.artist_members.append((group_id, member_id))
        self._s.log("music.add_artist_member")
        return True

    def totals(self):
        s = self._s
        return mu.MusicTotals(len(s.artists), len(s.venues), len(s.festivals), len(s.tracks), len(s.scrobbles))
