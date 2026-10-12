"""Application service for loading sources: store the records a reader produced, validate and store their
relations, and record the vault files facts point at. It talks to the build's `SourceRepository`
(`build_repositories_port`) and decides through `identity_service.SourceIdentity`; it knows no SQL, no file and no
step. `report` receives the lines the build prints (a warning for a malformed or duplicate identifier)."""
from typing import Callable, Iterable, List, Optional

import build_records as r
import identity_service
import source_status
from build_repositories_port import SourceRepository
from inputs_port import Inputs
from source_record import SourceRecord


def _quiet(_line: str) -> None:
    return None


class _Report:
    """The `Diagnostics` of `identity_service`, over the caller's `report` callable."""
    def __init__(self, report: Callable[[str], None]):
        self.emit = report


def new_source(record: SourceRecord) -> r.NewSource:
    """The `sources` row a record becomes: its fields, then its file fingerprint, status and custodial history."""
    fingerprint, state, acquisition = record.fingerprint or {}, record.state.columns(), record.acquisition

    def text(key):
        value = fingerprint.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"source {record.citekey!r}: fingerprint {key} must be text or null")
        return value

    size_bytes = fingerprint.get("size_bytes")
    if size_bytes is not None and (not isinstance(size_bytes, int) or isinstance(size_bytes, bool)):
        raise ValueError(f"source {record.citekey!r}: fingerprint size_bytes must be an integer or null")
    return r.NewSource(
        citekey=record.citekey, name=record.name, source_type=record.source_type, publisher=record.publisher,
        url=record.url, published_date=record.published_date, retrieved_date=record.retrieved_date,
        description=record.description, origin_path=record.origin_path,
        content_sha256=text("content_sha256"), size_bytes=size_bytes,
        file_mtime=text("file_mtime"), mime_type=text("mime_type"),
        file_state=text("file_state"),
        status=state["status"], status_date=state["status_date"], status_note=state["status_note"],
        edition=state["edition"], original_published_date=state["original_published_date"],
        acquired_at=acquisition.get("acquired_at"), acquired_via=acquisition.get("acquired_via"),
        where_from=acquisition.get("where_from"), acquired_note=acquisition.get("acquired_note"))


def add_sources(repo: SourceRepository, records: Iterable[SourceRecord], skip_known: bool = False,
                report: Callable[[str], None] = _quiet) -> int:
    """Store each record; returns how many were stored. With `skip_known` a record whose citekey is already stored
    (or appeared earlier in `records`) is skipped, which is how the citation notes yield to the manual sources."""
    identity = identity_service.SourceIdentity(repo, _Report(report))
    known = set(identity.citekeys()) if skip_known else set()
    stored = 0
    for record in records:
        if skip_known and record.citekey in known:
            continue
        for message in record.identifier_problems:
            report(f"  WARNING -- {message}; identifier not recorded")
        source_id = repo.add(new_source(record))
        identity.attach_identifiers(source_id, record.citekey, record.identifiers)
        for order, name in record.authors:
            repo.link_author(source_id, name, order)
        if record.citekey is not None:
            known.add(record.citekey)
        stored += 1
    return stored


def link_relations(repo: SourceRepository, relations: Iterable) -> List[source_status.Relation]:
    """Validate `relations` against the stored citekeys (see source_status.check_relations: unknown citekey, self,
    cycle) and store them. Raises SourceStatusError before anything is written. Returns what was stored."""
    ids = repo.citekeys()
    checked = source_status.check_relations(list(relations), set(ids))
    for source, relation, related in checked:
        repo.add_relation(r.NewSourceRelation(ids[source], relation, ids[related]))
    return checked


def vault_file_id(repo: SourceRepository, inputs: Inputs, path: Optional[str]) -> Optional[int]:
    """The vault_files id for `path`, its fixity record (#38) written on first sight. A file already recorded this
    build is not hashed again. A file that cannot be read is 'missing' with no hash; this never raises for a bad
    path. A vault file has no data record, so its custodial history (#46) comes from the file attributes alone."""
    return identity_service.SourceIdentity(repo, _Report(_quiet)).vault_file(path, inputs)
