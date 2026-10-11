"""In-memory fakes of the build repositories, for lane authors: run a bound step against a dict, no sqlite.

    state = FakeState()
    session = fake_session(state)           # a BuildSession over the fakes
    ...run a step bound to session.sources, session.facts, ...
    state.commits                           # how many times the unit of work was committed
    state.operations                        # ordered ("facts.add", ...) log: the allocation/ordering contract
    state.facts, state.sources, ...         # the stored records

What they model (and what a lane may rely on): ids are allocated per table from 1 in call order; a source citekey
and a fact source_key are unique (a duplicate raises sqlite3.IntegrityError, like the real table); `tally` is
ordered by value; get-or-create operations return the existing id for the same key. What they do not model: the
schema's CHECK constraints, foreign keys, the FTS tables, privacy resolution (`FakePrivacy.apply_rules` raises
the facts you queued with `state.privacy_raises`) and the revision-log validation (the log is whatever you put in
`state.revision_log`). A step that depends on those is covered by the sqlite characterization tests instead.
"""
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import build_records as r
from build_repositories_port import AliasRejected, BuildSession


@dataclass
class FakeState:
    operations: List[Tuple[str, ...]] = field(default_factory=list)
    commits: int = 0
    # sources
    sources: Dict[int, r.NewSource] = field(default_factory=dict)
    publishers: Dict[str, int] = field(default_factory=dict)
    authors: Dict[str, int] = field(default_factory=dict)
    source_authors: List[Tuple[int, int, int]] = field(default_factory=list)
    identifiers: Dict[Tuple[str, str], int] = field(default_factory=dict)
    identifier_conflicts: List[Tuple[int, str, str, int]] = field(default_factory=list)
    vault_files: Dict[str, int] = field(default_factory=dict)
    vault_file_states: Dict[int, Optional[str]] = field(default_factory=dict)
    vault_file_fixity: Dict[int, r.FileFixity] = field(default_factory=dict)
    relations: List[r.NewSourceRelation] = field(default_factory=list)
    # subjects
    subjects: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    aliases: Dict[str, int] = field(default_factory=dict)
    subjects_file: Optional[Sequence[Any]] = None
    # facts and revisions
    facts: Dict[int, r.NewFact] = field(default_factory=dict)
    fact_sources: List[r.FactSourceLink] = field(default_factory=list)
    fact_measurements: List[Tuple[int, str]] = field(default_factory=list)
    revisions: List[Tuple[int, Mapping[str, object]]] = field(default_factory=list)
    revision_log: List[Tuple[int, Mapping[str, object]]] = field(default_factory=list)
    write_backs: List[r.RevisionWriteBack] = field(default_factory=list)
    first_revisions: Dict[str, r.FirstRevision] = field(default_factory=dict)
    # privacy
    privacy_rules: Any = None
    privacy_raises: List[Tuple[int, str]] = field(default_factory=list)
    privacy_applied: List[Any] = field(default_factory=list)
    history_raised: int = 0
    # entities
    entities_file: Optional[Sequence[Any]] = None
    entities: Dict[int, Any] = field(default_factory=dict)
    fact_entities: List[Tuple[int, int]] = field(default_factory=list)
    fact_texts: List[r.FactText] = field(default_factory=list)
    # measurements
    metrics: Dict[int, r.NewMetric] = field(default_factory=dict)
    measurements: List[r.NewMeasurement] = field(default_factory=list)
    exercises: Dict[str, int] = field(default_factory=dict)
    foods: Dict[str, int] = field(default_factory=dict)
    muscles: Dict[str, int] = field(default_factory=dict)
    training_sets: List[r.NewTrainingSet] = field(default_factory=list)
    food_log: List[r.NewFoodLogEntry] = field(default_factory=list)
    meal_log: List[r.NewMealLogEntry] = field(default_factory=list)
    muscle_volume: List[r.NewMuscleVolume] = field(default_factory=list)
    # claims
    claims: Dict[int, r.NewClaim] = field(default_factory=dict)
    claim_facts: List[Tuple[int, str, str, Optional[str]]] = field(default_factory=list)
    # music
    import_sources: Dict[Optional[str], int] = field(default_factory=dict)
    artists: Dict[int, str] = field(default_factory=dict)
    venues: Dict[Tuple[str, Optional[str]], int] = field(default_factory=dict)
    festivals: Dict[str, int] = field(default_factory=dict)
    attendances: List[r.NewAttendance] = field(default_factory=list)
    albums: List[r.NewAlbum] = field(default_factory=list)
    tracks: Dict[int, Tuple[int, str, Optional[str]]] = field(default_factory=dict)
    scrobbles: List[r.NewScrobble] = field(default_factory=list)
    artist_members: List[Tuple[int, int]] = field(default_factory=list)

    def log(self, *op: str) -> None:
        self.operations.append(op)


def _next(table: Mapping[int, Any]) -> int:
    return max(table, default=0) + 1


class FakeUnitOfWork:
    def __init__(self, state: FakeState):
        self._s = state

    def commit(self) -> None:
        self._s.commits += 1
        self._s.log("commit")


class FakeSources:
    def __init__(self, state: FakeState):
        self._s = state

    def citekeys(self):
        return {s.citekey: r.SourceId(i) for i, s in self._s.sources.items() if s.citekey is not None}

    def find(self, citekey):
        return self.citekeys().get(citekey)

    def add(self, source):
        s = self._s
        if source.citekey is not None and source.citekey in self.citekeys():
            raise sqlite3.IntegrityError("UNIQUE constraint failed: sources.citekey")
        if source.publisher and source.publisher not in s.publishers:
            s.publishers[source.publisher] = len(s.publishers) + 1
        sid = r.SourceId(_next(s.sources))
        s.sources[sid] = source
        s.log("sources.add", str(source.citekey))
        return sid

    def identifier_owner(self, scheme, value):
        owner = self._s.identifiers.get((scheme, value))
        if owner is None:
            return None
        return r.IdentifierOwner(r.SourceId(owner), self._s.sources[owner].citekey)

    def add_identifier(self, source_id, scheme, value):
        self._s.identifiers[(scheme, value)] = source_id
        self._s.log("sources.add_identifier", scheme, value)

    def record_identifier_conflict(self, source_id, scheme, value, owner_source_id):
        row = (source_id, scheme, value, owner_source_id)
        if row not in self._s.identifier_conflicts:
            self._s.identifier_conflicts.append(row)
        self._s.log("sources.record_identifier_conflict", scheme, value)

    def link_author(self, source_id, name, order=0):
        s = self._s
        author_id = s.authors.setdefault(name, len(s.authors) + 1)
        if not any(a[:2] == (source_id, author_id) for a in s.source_authors):
            s.source_authors.append((source_id, author_id, order))
        s.log("sources.link_author", name, str(order))

    def vault_file(self, path):
        fid = self._s.vault_files.get(path)
        return None if fid is None else r.VaultFileState(r.VaultFileId(fid), self._s.vault_file_states[fid])

    def add_vault_file(self, path):
        s = self._s
        fid = len(s.vault_files) + 1
        s.vault_files[path] = fid
        s.vault_file_states[fid] = None
        s.log("sources.add_vault_file", path)
        return r.VaultFileId(fid)

    def record_vault_file_fixity(self, file_id, fixity):
        self._s.vault_file_fixity[file_id] = fixity
        self._s.vault_file_states[file_id] = fixity.file_state
        self._s.log("sources.record_vault_file_fixity", str(file_id))

    def add_relation(self, relation):
        self._s.relations.append(relation)
        self._s.log("sources.add_relation", relation.relation)

    def count_by_status(self):
        out: Dict[str, int] = {}
        for src in self._s.sources.values():
            out[src.status] = out.get(src.status, 0) + 1
        return dict(sorted(out.items()))


class FakeSubjects:
    def __init__(self, state: FakeState):
        self._s = state

    def find_by_name(self, name):
        for i, row in self._s.subjects.items():
            if row["name"] == name:
                return r.SubjectId(i)
        return None

    def find_by_alias(self, alias):
        found = self._s.aliases.get(alias)
        return None if found is None else r.SubjectId(found)

    def add(self, name, domain=None):
        sid = r.SubjectId(_next(self._s.subjects))
        self._s.subjects[sid] = {"name": name, "domain": domain or "health-and-fitness", "parent_id": None,
                                 "parent_relation": "broader", "description": None, "deprecated": 0,
                                 "replaced_by": None}
        self._s.log("subjects.add", name)
        return sid

    def ensure(self, name, domain):
        if self.find_by_name(name) is None:
            self.add(name, domain)

    def ids_by_name(self):
        return {row["name"]: r.SubjectId(i) for i, row in self._s.subjects.items()}

    def set_hierarchy(self, subject_id, parent_id, relation, description, deprecated):
        row = self._s.subjects[subject_id]
        row.update(parent_id=parent_id, parent_relation=relation, description=description, deprecated=1 if deprecated else 0)
        self._s.log("subjects.set_hierarchy", row["name"])

    def set_replacement(self, subject_id, replaced_by_id):
        self._s.subjects[subject_id]["replaced_by"] = replaced_by_id
        self._s.log("subjects.set_replacement", self._s.subjects[subject_id]["name"])

    def add_alias(self, subject_id, alias):
        names = {row["name"] for row in self._s.subjects.values()}
        if alias in names or alias in self._s.aliases:
            raise AliasRejected("UNIQUE constraint failed: subject_aliases.alias")
        self._s.aliases[alias] = subject_id
        self._s.log("subjects.add_alias", alias)

    def count_with_parent(self):
        return sum(1 for row in self._s.subjects.values() if row["parent_id"] is not None)

    def count_aliases(self):
        return len(self._s.aliases)

    def count_deprecated(self):
        return sum(1 for row in self._s.subjects.values() if row["deprecated"])

    def read_file(self, path):
        return self._s.subjects_file


class FakeFacts:
    def __init__(self, state: FakeState):
        self._s = state

    def source_key_exists(self, source_key):
        return any(f.source_key == source_key for f in self._s.facts.values())

    def find_by_source_key(self, source_key):
        for i, f in self._s.facts.items():
            if f.source_key == source_key:
                return r.FactId(i)
        return None

    def add(self, fact):
        if self.source_key_exists(fact.source_key):
            raise sqlite3.IntegrityError("UNIQUE constraint failed: facts.source_key")
        fid = r.FactId(_next(self._s.facts))
        self._s.facts[fid] = fact
        self._s.log("facts.add", fact.source_key)
        return fid

    def link_source(self, link):
        if not any((x.fact_id, x.source_id) == (link.fact_id, link.source_id) for x in self._s.fact_sources):
            self._s.fact_sources.append(link)
        self._s.log("facts.link_source", str(link.fact_id))

    def link_measurements(self, fact_id, metric_key):
        metric_ids = {i for i, m in self._s.metrics.items() if m.key == metric_key}
        n = sum(1 for m in self._s.measurements if m.metric_id in metric_ids)
        self._s.fact_measurements.extend([(fact_id, metric_key)] * n)
        self._s.log("facts.link_measurements", str(fact_id), metric_key)
        return n

    def tally(self, by):
        out: Dict[str, int] = {}
        for f in self._s.facts.values():
            key = getattr(f, by)
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))


class FakeRevisions:
    def __init__(self, state: FakeState):
        self._s = state

    def insert(self, fact_id, revision):
        self._s.revisions.append((fact_id, revision))
        self._s.log("revisions.insert", str(fact_id))

    def fact_ids_by_source_key(self):
        return {f.source_key: r.FactId(i) for i, f in self._s.facts.items()}

    def first_revisions(self):
        return dict(self._s.first_revisions)

    def read_log(self, path, base):
        self._s.log("revisions.read_log", path)
        return list(self._s.revision_log)

    def write_back(self, write):
        self._s.write_backs.append(write)
        self._s.log("revisions.write_back", str(write.fact_id))

    def count(self):
        return len(self._s.revisions)


class FakePrivacy:
    def __init__(self, state: FakeState):
        self._s = state

    def load_rules(self, path):
        self._s.log("privacy.load_rules", path)
        return self._s.privacy_rules

    def apply_rules(self, rules):
        self._s.privacy_applied.append(rules)
        self._s.log("privacy.apply_rules")
        raised = tuple((r.FactId(i), why) for i, why in self._s.privacy_raises)
        return r.PrivacyOutcome(raised, 0)

    def raise_history_of_private_facts(self):
        self._s.history_raised += 1
        self._s.log("privacy.raise_history_of_private_facts")


class FakeEntities:
    def __init__(self, state: FakeState):
        self._s = state

    def read_file(self, path):
        return self._s.entities_file

    def add(self, entity):
        eid = r.EntityRowId(_next(self._s.entities))
        self._s.entities[eid] = entity
        self._s.log("entities.add", entity.id)
        return eid

    def fact_texts(self):
        return list(self._s.fact_texts)

    def link_fact(self, fact_id, entity_row_id):
        self._s.fact_entities.append((fact_id, entity_row_id))
        self._s.log("entities.link_fact", str(fact_id))

    def fact_visibility(self, fact_id):
        fact = self._s.facts.get(fact_id)
        return None if fact is None else fact.visibility


class FakeMeasurements:
    def __init__(self, state: FakeState):
        self._s = state

    def find_metric(self, key):
        for i, m in self._s.metrics.items():
            if m.key == key:
                return r.MetricId(i)
        return None

    def add_metric(self, metric):
        mid = r.MetricId(_next(self._s.metrics))
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
        return r.MeasurementTotals(len(s.measurements), len(s.training_sets), len(s.food_log), len(s.meal_log),
                                   len(s.muscle_volume))


class FakeClaims:
    def __init__(self, state: FakeState):
        self._s = state

    def add(self, claim):
        cid = r.ClaimId(_next(self._s.claims))
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
    def __init__(self, state: FakeState):
        self._s = state

    def import_source(self, path):
        s = self._s
        if path not in s.import_sources:
            s.import_sources[path] = len(s.import_sources) + 1
        return r.ImportSourceId(s.import_sources[path])

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
        return [(r.ArtistId(i), n) for i, n in self._s.artists.items()]

    def add_artist(self, name):
        aid = r.ArtistId(_next(self._s.artists))
        self._s.artists[aid] = name
        self._s.log("music.add_artist", name)
        return aid

    def venue(self, name, city_state):
        key = (name, city_state)
        if key not in self._s.venues:
            self._s.venues[key] = len(self._s.venues) + 1
        return r.VenueId(self._s.venues[key])

    def festival(self, name):
        if name not in self._s.festivals:
            self._s.festivals[name] = len(self._s.festivals) + 1
        return r.FestivalId(self._s.festivals[name])

    def add_attendance(self, attendance):
        self._s.attendances.append(attendance)
        self._s.log("music.add_attendance")

    def add_album(self, album):
        self._s.albums.append(album)
        self._s.log("music.add_album")

    def tracks(self):
        return [(r.TrackId(i), r.ArtistId(a), t, al) for i, (a, t, al) in self._s.tracks.items()]

    def add_track(self, artist_id, title, album):
        tid = r.TrackId(_next(self._s.tracks))
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
        return r.MusicTotals(len(s.artists), len(s.venues), len(s.festivals), len(s.tracks), len(s.scrobbles))


def fake_session(state: Optional[FakeState] = None) -> BuildSession:
    """A BuildSession over in-memory fakes sharing `state` (a fresh one when omitted; keep your own reference to
    inspect it)."""
    s = state if state is not None else FakeState()
    return BuildSession(sources=FakeSources(s), subjects=FakeSubjects(s), facts=FakeFacts(s), revisions=FakeRevisions(s),
                        privacy=FakePrivacy(s), entities=FakeEntities(s), measurements=FakeMeasurements(s),
                        claims=FakeClaims(s), music=FakeMusic(s), unit_of_work=FakeUnitOfWork(s))


class FakeSessionOpener:
    """A SessionOpener (build_composition) over a FakeState; records what it was asked to open and closes."""
    def __init__(self, state: Optional[FakeState] = None):
        self.state = state if state is not None else FakeState()
        self.opened: List[str] = []
        self.closed = 0

    @contextmanager
    def open(self, target: str) -> Iterator[BuildSession]:
        self.opened.append(target)
        try:
            yield fake_session(self.state)
        finally:
            self.closed += 1


class FakeVault:
    """VaultReader over `tables` (table name -> list of row dicts) and `columns` (table -> column names, for the
    two column queries). Used as its own opener: `FakeVault(...).open(path)` returns itself."""
    def __init__(self, tables=None, columns=None):
        self.tables = dict(tables or {})
        self.columns = dict(columns or {})
        self.closed = False
        self.opened: List[str] = []

    def open(self, notes_vault):
        self.opened.append(notes_vault)
        return self

    def _rows(self, table):
        return list(self.tables.get(table, []))

    def dexa_scan(self, date):
        return next((row for row in self._rows("bodycomp_dexa") if row["date"] == date), None)

    def bloodwork(self):
        return self._rows("bloodwork")

    def body_measurements(self):
        return self._rows("body_measurements")

    def latest_strength_checkpoint(self):
        rows = sorted(self._rows("strength_checkpoints"), key=lambda row: row["date"], reverse=True)
        return rows[0] if rows else None

    def nutrition_daily(self):
        return self._rows("nutrition_daily")

    def scale_readings(self):
        return self._rows("scale_readings")

    def renpho_scale_readings(self):
        return self._rows("renpho_scale_readings")

    def muscle_volume_columns(self):
        return [c for c in self.columns.get("muscle_volume_weekly", []) if c != "date"]

    def muscle_volume_weekly(self):
        return self._rows("muscle_volume_weekly")

    def workout_sets(self):
        return self._rows("workout_sets")

    def jefit_exercise_sets(self):
        return self._rows("jefit_exercise_sets")

    def mfp_measurements(self):
        return self._rows("mfp_measurements")

    def mfp_exercise_log(self):
        return self._rows("mfp_exercise_log")

    def mfp_nutrition_log(self):
        return self._rows("mfp_nutrition_log")

    def nutrition_food_log(self):
        return self._rows("nutrition_food_log")

    def micronutrient_columns(self):
        return [c for c in self.columns.get("micronutrients", []) if c not in ("date", "source_file")]

    def micronutrients(self):
        return self._rows("micronutrients")

    def close(self):
        self.closed = True


class CollectingDiagnostics:
    """Diagnostics that keeps the lines (what `print` shows in production)."""
    def __init__(self):
        self.lines: List[str] = []

    def emit(self, line: str) -> None:
        self.lines.append(line)


class FakeInputs:
    """inputs_port.Inputs over dicts: `files` maps a path to text, `json_files` to a decoded value, `fingerprints`
    and `acquisitions` to the dicts the real adapters return. An unknown path is 'missing' like the real one."""
    def __init__(self, files=None, json_files=None, fingerprints=None, acquisitions=None, directories=None):
        self.files = dict(files or {})
        self.json_files = dict(json_files or {})
        self.fingerprints = dict(fingerprints or {})
        self.acquisitions = dict(acquisitions or {})
        self.directories = dict(directories or {})

    def exists(self, path):
        return path in self.files or path in self.json_files

    def read_text(self, path, encoding="utf-8"):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def read_json(self, path):
        if path not in self.json_files:
            raise FileNotFoundError(path)
        return self.json_files[path]

    def list_files(self, directory, pattern):
        return sorted(self.directories.get(directory, []))

    def open_database(self, path):
        raise NotImplementedError("a fake has no vault database; give the step the rows another way")

    def fingerprint(self, path):
        return self.fingerprints.get(path, {"content_sha256": None, "size_bytes": None, "file_mtime": None,
                                            "mime_type": None, "file_state": "missing"})

    def acquisition(self, row, path):
        return {**row, **self.acquisitions.get(path, {})}
