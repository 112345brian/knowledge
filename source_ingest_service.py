"""Application service for loading sources: store the records a reader produced, validate and store their
relations, and record the vault files facts point at. It talks to a `SourceRepository`; it knows no SQL, no file and
no step. `report` receives the lines the build prints (a warning for a malformed or duplicate identifier)."""
from typing import Callable, Iterable, List, Optional

import acquisition_rules
import source_status
from inputs_port import Inputs
from source_record import NO_ACQUISITION, SourceRecord
from sources_port import SourceRepository


def _quiet(_line: str) -> None:
    return None


def add_sources(repo: SourceRepository, records: Iterable[SourceRecord], skip_known: bool = False,
                report: Callable[[str], None] = _quiet) -> int:
    """Store each record; returns how many were stored. With `skip_known` a record whose citekey is already stored
    (or appeared earlier in `records`) is skipped, which is how the citation notes yield to the manual sources."""
    known = repo.citekeys() if skip_known else set()
    stored = 0
    for record in records:
        if skip_known and record.citekey in known:
            continue
        for message in record.identifier_problems:
            report(f"  WARNING -- {message}; identifier not recorded")
        for c in repo.add_source(record):
            report(f"  WARNING -- duplicate identifier {c.scheme}:{c.value} shared by sources {c.owner!r} and "
                   f"{record.citekey!r} (not merged)")
        known.add(record.citekey)
        stored += 1
    return stored


def link_relations(repo: SourceRepository, relations: Iterable) -> List[source_status.Relation]:
    """Validate `relations` against the stored citekeys (see source_status.check_relations: unknown citekey, self,
    cycle) and store them. Raises SourceStatusError before anything is written. Returns what was stored."""
    checked = source_status.check_relations(list(relations), repo.citekeys())
    repo.add_relations(checked)
    return checked


def vault_file_id(repo: SourceRepository, inputs: Inputs, path: Optional[str]) -> Optional[int]:
    """The vault_files id for `path`, its fixity record (#38) written on first sight. A file already recorded this
    build is not hashed again. A file that cannot be read is 'missing' with no hash; this never raises for a bad
    path. A vault file has no data record, so its custodial history (#46) comes from the file attributes alone."""
    if not path:
        return None
    recorded = repo.recorded_vault_file(path)
    if recorded is not None:
        return recorded
    return repo.record_vault_file(path, inputs.fingerprint(path), inputs.acquisition(dict(NO_ACQUISITION), path))
