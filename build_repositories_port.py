"""Ports a build step uses to read and write the database being built: one repository per aggregate.

Domain service ring: protocols and one value (`BuildSession`), no I/O. A method is one decision-free persistence
operation in the language of the knowledge base ("find the subject called X", "add this fact") that takes and
returns the records of `build_records`. There is no `execute`, no cursor, no row, no SQL text on any port: a
decision (skip this item, create the subject, keep the first owner of an identifier) is made by the step or by
`identity_service`, and the adapter only stores what it is told. Infrastructure implements these; the
application and the steps depend only on these protocols, so a test hands them the in-memory fakes in
tests/fake_build_repositories.py.

`read_*`/`load_*` methods are the document readers that used to be `step_stores_port.StepStores` (a data file
is parsed into the model's values by the same store that writes its table); they take a path from `Locations`.

Ordering contract every adapter keeps, because ids are allocated in insertion order and the golden dump compares
them: each `add`/`get-or-create` allocates its row when called, one at a time, in call order.

The unit of work is the build's one connection: a step calls `commit()` exactly where it commits today (see
docs/architecture.md CONTRACTS and tests/test_build_characterization.py for where that is, per step).
"""
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Optional, Protocol, Sequence, Tuple

import build_records as r
import entity_rules
import privacy
import subject_rules

FactTally = Literal["freshness", "trust_level", "is_personal"]


class AliasRejected(Exception):
    """The alias equals a subject name or another alias (the table's uniqueness refused it); `reason` is the
    store's own message, which the step puts into its error."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UnitOfWork(Protocol):
    def commit(self) -> None: ...


class SourceRepository(Protocol):
    def citekeys(self) -> Mapping[str, r.SourceId]:
        """citekey -> id for every source that has a citekey, as of this call (a snapshot, not a live view)."""
    def find(self, citekey: str) -> Optional[r.SourceId]: ...
    def add(self, source: r.NewSource) -> r.SourceId:
        """Get-or-create the publisher (when named), then insert the row. A duplicate citekey raises."""
    def identifier_owner(self, scheme: str, value: str) -> Optional[r.IdentifierOwner]: ...
    def add_identifier(self, source_id: r.SourceId, scheme: str, value: str) -> None: ...
    def record_identifier_conflict(self, source_id: r.SourceId, scheme: str, value: str,
                                   owner_source_id: r.SourceId) -> None:
        """INSERT OR IGNORE into source_identifier_conflicts: recording the same conflict twice is not an error."""
    def link_author(self, source_id: r.SourceId, name: str, order: int = 0) -> None:
        """Get-or-create the author by name, then INSERT OR IGNORE the (source, author) pair at `order`."""
    def vault_file(self, path: str) -> Optional[r.VaultFileState]: ...
    def add_vault_file(self, path: str) -> r.VaultFileId: ...
    def record_vault_file_fixity(self, file_id: r.VaultFileId, fixity: r.FileFixity) -> None: ...
    def add_relation(self, relation: r.NewSourceRelation) -> None: ...
    def count_by_status(self) -> Mapping[str, int]: ...


class SubjectRepository(Protocol):
    def find_by_name(self, name: str) -> Optional[r.SubjectId]: ...
    def find_by_alias(self, alias: str) -> Optional[r.SubjectId]:
        """The canonical subject an alias belongs to."""
    def add(self, name: str, domain: Optional[str] = None) -> r.SubjectId:
        """Insert a subject; `domain` None leaves the column to the schema default."""
    def ensure(self, name: str, domain: str) -> None:
        """INSERT OR IGNORE: an existing subject keeps the domain it has."""
    def ids_by_name(self) -> Mapping[str, r.SubjectId]: ...
    def set_hierarchy(self, subject_id: r.SubjectId, parent_id: Optional[r.SubjectId], relation: str,
                      description: Optional[str], deprecated: bool) -> None: ...
    def set_replacement(self, subject_id: r.SubjectId, replaced_by_id: r.SubjectId) -> None: ...
    def add_alias(self, subject_id: r.SubjectId, alias: str) -> None:
        """Raises AliasRejected when the alias is not unique."""
    def count_with_parent(self) -> int: ...
    def count_aliases(self) -> int: ...
    def count_deprecated(self) -> int: ...
    def read_file(self, path: str) -> Optional[Sequence[subject_rules.Subject]]:
        """subjects.json as values, None when the file is absent; a bad file raises subject_rules.SubjectsError."""


class FactRepository(Protocol):
    def source_key_exists(self, source_key: str) -> bool: ...
    def find_by_source_key(self, source_key: str) -> Optional[r.FactId]: ...
    def add(self, fact: r.NewFact) -> r.FactId: ...
    def link_source(self, link: r.FactSourceLink) -> None:
        """INSERT OR IGNORE into fact_sources."""
    def link_measurements(self, fact_id: r.FactId, metric_key: str) -> int:
        """One fact_measurements row per measurement of the metric; returns how many were linked."""
    def tally(self, by: FactTally) -> Mapping[Any, int]:
        """fact count per value of the column, ordered by value (what `GROUP BY` returns)."""


class RevisionRepository(Protocol):
    def insert(self, fact_id: r.FactId, revision: Mapping[str, object]) -> None:
        """One fact_revisions row from a full revision snapshot (revisions.REVISION_KEYS)."""
    def fact_ids_by_source_key(self) -> Mapping[str, r.FactId]: ...
    def first_revisions(self) -> Mapping[str, r.FirstRevision]: ...
    def read_log(self, path: str, base: Mapping[str, Mapping[str, object]]) -> Sequence[Tuple[int, Mapping[str, object]]]:
        """[(line number, record)]; a missing file is an empty log, a bad line raises RevisionError naming it."""
    def write_back(self, write: r.RevisionWriteBack) -> None:
        """Write a fact's latest revision into its `facts` row (mutable fields only)."""
    def count(self) -> int: ...


class PrivacyRepository(Protocol):
    def load_rules(self, path: str) -> privacy.Rules:
        """The privacy rules file; an absent file is empty rules, a corrupt one raises."""
    def apply_rules(self, rules: privacy.Rules) -> r.PrivacyOutcome:
        """Raise-only re-application to every fact; also tags subjects. Does not commit."""
    def raise_history_of_private_facts(self) -> None:
        """Every revision snapshot of a private fact becomes private too (history is never more visible)."""


class EntityRepository(Protocol):
    def read_file(self, path: str) -> Optional[Sequence[entity_rules.Entity]]:
        """entities.json as values, None when the file is absent; a bad file raises."""
    def add(self, entity: entity_rules.Entity) -> r.EntityRowId:
        """The entity row and its alias rows (normalized names)."""
    def fact_texts(self) -> Iterable[r.FactText]: ...
    def link_fact(self, fact_id: r.FactId, entity_row_id: r.EntityRowId) -> None: ...
    def fact_visibility(self, fact_id: r.FactId) -> Optional[str]: ...


class MeasurementRepository(Protocol):
    def find_metric(self, key: str) -> Optional[r.MetricId]: ...
    def add_metric(self, metric: r.NewMetric) -> r.MetricId: ...
    def add(self, measurement: r.NewMeasurement) -> None: ...
    def exercise(self, name: str) -> int:
        """Get-or-create."""
    def food(self, name: str) -> int:
        """Get-or-create."""
    def muscle(self, name: str) -> int:
        """Get-or-create."""
    def add_training_set(self, training_set: r.NewTrainingSet) -> None: ...
    def add_food_log_entry(self, entry: r.NewFoodLogEntry) -> None: ...
    def add_meal_log_entry(self, entry: r.NewMealLogEntry) -> None: ...
    def add_muscle_volume(self, volume: r.NewMuscleVolume) -> None: ...
    def totals(self) -> r.MeasurementTotals: ...


class ClaimRepository(Protocol):
    def add(self, claim: r.NewClaim) -> r.ClaimId: ...
    def link_facts_by_prefix(self, claim_id: r.ClaimId, prefix: str, role: str, note: Optional[str]) -> int:
        """claim_facts rows for every fact whose statement starts with `prefix` (LIKE, so `%`/`_` in a prefix are
        wildcards, as today); returns how many were linked."""


class MusicRepository(Protocol):
    def import_source(self, path: Optional[str]) -> r.ImportSourceId:
        """Get-or-create the import_sources row for an input file."""
    def delete_attendances(self, import_source_id: r.ImportSourceId) -> None: ...
    def delete_albums(self, import_source_id: r.ImportSourceId) -> None: ...
    def delete_scrobbles(self, import_source_id: r.ImportSourceId) -> None: ...
    def artists(self) -> Iterable[Tuple[r.ArtistId, str]]:
        """(id, name) of every artist, in table order."""
    def add_artist(self, name: str) -> r.ArtistId: ...
    def venue(self, name: str, city_state: Optional[str]) -> r.VenueId:
        """Get-or-create on (name, city_state); NULL matches NULL."""
    def festival(self, name: str) -> r.FestivalId:
        """Get-or-create."""
    def add_attendance(self, attendance: r.NewAttendance) -> None: ...
    def add_album(self, album: r.NewAlbum) -> None: ...
    def tracks(self) -> Iterable[Tuple[r.TrackId, r.ArtistId, str, Optional[str]]]:
        """(id, artist id, title, album) of every track."""
    def add_track(self, artist_id: r.ArtistId, title: str, album: Optional[str]) -> r.TrackId: ...
    def add_scrobbles(self, scrobbles: Sequence[r.NewScrobble]) -> None:
        """INSERT OR IGNORE, in the order given."""
    def add_artist_member(self, group_id: r.ArtistId, member_id: r.ArtistId) -> bool:
        """INSERT OR IGNORE; True when a row was added."""
    def totals(self) -> r.MusicTotals: ...


@dataclass(frozen=True)
class BuildSession:
    """The repositories and unit of work of one build, over one database. Built by the composition root's session
    factory; a step is bound to the members it declares and never sees the session whole."""
    sources: SourceRepository
    subjects: SubjectRepository
    facts: FactRepository
    revisions: RevisionRepository
    privacy: PrivacyRepository
    entities: EntityRepository
    measurements: MeasurementRepository
    claims: ClaimRepository
    music: MusicRepository
    unit_of_work: UnitOfWork


REPOSITORY_PORTS = {
    "sources": SourceRepository, "subjects": SubjectRepository, "facts": FactRepository,
    "revisions": RevisionRepository, "privacy": PrivacyRepository, "entities": EntityRepository,
    "measurements": MeasurementRepository, "claims": ClaimRepository, "music": MusicRepository,
    "unit_of_work": UnitOfWork,
}
