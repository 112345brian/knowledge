"""Composed source readers: objects that yield normalized `SourceRecord`s from manual_sources.json and from the
vault's citation notes, so the steps that load sources and the step that links their relations share one parser.

`ManualSources` and `VaultNotes` each read their origin through `ctx.inputs` on every call; `Composed` is several
of them in order (manual first). Nothing is cached: a caller that asks twice reads the files twice, which is what
link_source_relations relies on (it sees the files as they are when it runs, not a snapshot from step 01/02).
"""
import os
from typing import List

import acquisition_rules
import identifiers
import source_status
from ingest import source_ingest_rules
from source_record import SourceRecord
from source_status import Relation, SourceState

MANUAL_FILE = "manual_sources.json"
NOTE_RETRIEVED_DATE = "2026-09-11"   # what every citation note's retrieved_date has always been recorded as
_ID_FIELDS = ("doi", "pmid", "pmcid", "isbn", "issn", "arxiv")


def _identifiers(raw, where):
    problems = []
    pairs = identifiers.collect(raw, where, problems)
    return tuple(pairs), tuple(problems)


class ManualSources:
    """The hand-authored sources of `<private data>/manual_sources.json`. An origin_path may carry {VAULT} / {HEALTH}."""

    def __init__(self, inputs, locations):
        self._inputs, self._locations = inputs, locations

    def _items(self):
        loc = self._locations
        items = self._inputs.read_json(os.path.join(loc.private_data_dir, MANUAL_FILE))
        for s in items:
            if s.get("origin_path"):
                s["origin_path"] = source_ingest_rules.format_origin_path(s["origin_path"], loc.notes_vault, loc.health_dir)
        return items

    def records(self) -> List[SourceRecord]:
        out = []
        for s in self._items():
            where = f"{MANUAL_FILE}: source {s.get('citekey')!r}"
            raw_ids = {**{k: s.get(k) for k in _ID_FIELDS}, **(s.get("identifiers") or {})}
            pairs, problems = _identifiers(raw_ids, where)
            out.append(SourceRecord(
                citekey=s.get("citekey"), name=s.get("name"), source_type=s.get("source_type"),
                state=SourceState.from_raw(s, where), publisher=s.get("publisher"), url=s.get("url"),
                published_date=s.get("published_date"), retrieved_date=s.get("retrieved_date"),
                description=s.get("description"), origin_path=s.get("origin_path"),
                acquisition=self._inputs.acquisition(acquisition_rules.normalize_data(s, where), s.get("origin_path")),  # #46: data first, file attributes fill gaps
                authors=source_ingest_rules.byline(s.get("author")),
                identifiers=pairs, identifier_problems=problems,
                relations=tuple(Relation(s["citekey"], rel, t) for rel, key in (("replaces", "replaces"), ("is-version-of", "is_version_of"))
                                for t in source_status.as_list(s.get(key))),
            ))
        return out

    def relations(self) -> List[Relation]:
        """Only the `replaces` / `is_version_of` lists: nothing else of a manual source is validated here."""
        rels = []
        for s in self._items():
            for rel, key in (("replaces", "replaces"), ("is-version-of", "is_version_of")):
                rels.extend(Relation(s["citekey"], rel, t) for t in source_status.as_list(s.get(key)))
        return rels


class VaultNotes:
    """The citation notes (`sources/*.md`) of the notes vault, if one is configured; without a vault there are none."""

    def __init__(self, inputs, locations):
        self._inputs, self._locations = inputs, locations

    def records(self) -> List[SourceRecord]:
        vault, inputs = self._locations.notes_vault, self._inputs
        if not vault:
            return []
        out = []
        for fp in inputs.list_files(os.path.join(vault, "sources"), "*.md"):
            base = os.path.basename(fp)
            if base == "README.md":
                continue
            row = source_ingest_rules.note_to_source_row(base, inputs.read_text(fp), vault)
            where = row["where"]
            pairs, problems = _identifiers(row["raw_identifiers"], where)
            acquisition = inputs.acquisition(acquisition_rules.normalize_data(row["raw_acquired"], where), fp)  # #46
            fingerprint = inputs.fingerprint(fp)  # #38
            out.append(SourceRecord(
                citekey=row["citekey"], name=row["name"], source_type=row["source_type"],
                state=SourceState.from_raw(row, where), publisher=row["publisher"], url=row["url"],
                published_date=row["published_date"], retrieved_date=NOTE_RETRIEVED_DATE,
                description=row["description"], origin_path=row["origin_path"],
                acquisition=acquisition, fingerprint=fingerprint,
                authors=source_ingest_rules.byline(row["author"]),
                identifiers=pairs, identifier_problems=problems,
                relations=tuple(Relation(*r) for r in row["relations"]),
            ))
        return out

    def relations(self) -> List[Relation]:
        return [r for record in self.records() for r in record.relations]


class Composed:
    """Several readers as one: records and relations in the order the readers were given."""

    def __init__(self, *readers):
        self._readers = readers

    def records(self) -> List[SourceRecord]:
        return [r for reader in self._readers for r in reader.records()]

    def relations(self) -> List[Relation]:
        return [r for reader in self._readers for r in reader.relations()]


def default_readers(ctx) -> Composed:
    """Manual sources, then the vault notes: the order the build loads them in."""
    return Composed(ManualSources(ctx.inputs, ctx.locations), VaultNotes(ctx.inputs, ctx.locations))
