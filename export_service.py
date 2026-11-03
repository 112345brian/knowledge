"""Export the database to deterministic text files and check them against it (`import_service` reads them back).

Application ring (tach layer `application`): it orders the work and names the failures through the ports in
`export_port`; `export_rules` decides the format. It never prints or exits.

`export_database` computes every text of a part before it writes anything of it, and writes the part's manifest last,
so a failure leaves either the old part or files the old manifest still describes. The parts are independent: the
core part (small, versioned in git) can be written without the bulk part (large client data) and the notes.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

import export_rules as rules
from export_port import ExportFiles, ExportReads, ExportScope, NotesFiles

DEFAULT_PARTS = (rules.CORE, rules.BULK)


@dataclass(frozen=True)
class ExportDeps:
    """What `export_database` and `verify_export` reach the world through."""
    export_reads: ExportReads
    export_files: ExportFiles
    export_scope: ExportScope
    notes_source: NotesFiles


def _check_parts(parts: Sequence[str]) -> Sequence[str]:
    unknown = [p for p in parts if p not in (rules.CORE, rules.BULK, rules.NOTES)]
    if unknown or not parts:
        raise rules.ExportError(f"parts must be some of core, bulk, notes (got {list(parts)})")
    return parts


def _table_texts(deps: ExportDeps, part: str) -> Dict[str, str]:
    existing = deps.export_reads.tables()
    if part == rules.CORE:
        missing = [t for t in rules.CORE_TABLES if t not in existing]
        if missing:
            raise rules.ExportError("the database lacks core table(s): " + ", ".join(missing))
        tables: List[str] = list(rules.CORE_TABLES)
    else:
        tables = deps.export_scope.bulk_tables(existing)
    return {t: rules.encode_table(deps.export_reads.rows(t)) for t in tables}


def _note_texts(deps: ExportDeps) -> Dict[str, str]:
    if not deps.notes_source.names():
        raise rules.ExportError("there are no notes to export at the configured notes directory")
    return {rules.check_note(n): deps.notes_source.read(n) for n in deps.notes_source.names()}


def _texts(deps: ExportDeps, part: str) -> Dict[str, str]:
    return _note_texts(deps) if part == rules.NOTES else _table_texts(deps, part)


def _path(part: str, name: str) -> str:
    return rules.note_path(name) if part == rules.NOTES else rules.file_name(part, name)


def export_database(deps: ExportDeps, parts: Sequence[str] = DEFAULT_PARTS) -> Mapping[str, Mapping[str, int]]:
    """Write the given parts (core, bulk, notes) of the export. Returns {part: {item: rows}} (a note counts 1)."""
    computed = {part: _texts(deps, part) for part in _check_parts(parts)}     # everything first, then any write
    version = deps.export_reads.user_version()
    result: Dict[str, Mapping[str, int]] = {}
    for part, texts in computed.items():
        for name, text in texts.items():
            deps.export_files.write(_path(part, name), text)
        deps.export_files.write(rules.manifest_name(part), rules.build_manifest(version, texts))
        result[part] = {n: (1 if part == rules.NOTES else rules.line_count(t)) for n, t in texts.items()}
    return result


def verify_export(deps: ExportDeps, parts: Sequence[str] = DEFAULT_PARTS) -> List[str]:
    """Where the export files and the database disagree, as lines (empty: the files are exactly what an export of the
    database would write now). Writes nothing. A requested part that was never exported is a problem."""
    problems: List[str] = []
    version = deps.export_reads.user_version()
    for part in _check_parts(parts):
        try:
            manifest = rules.read_manifest(deps.export_files.read, part)
        except rules.ExportError as e:
            problems.append(str(e))
            continue
        if manifest.user_version != version:
            problems.append(f"{part}: schema version: export {manifest.user_version}, database {version}")
        on_disk = rules.read_texts(deps.export_files.read, part, manifest)
        problems += [f"{part}/{p}" for p in rules.drift(manifest, on_disk)]
        current = _texts(deps, part)
        for name in sorted(set(current) | set(manifest.tables)):
            if name not in manifest.tables:
                problems.append(f"{part}/{name}: is in the database but not in the export")
            elif name not in current:
                problems.append(f"{part}/{name}: is in the export but not in the database")
            elif name in on_disk and on_disk[name] != current[name]:
                problems.append(f"{part}/{name}: the database has changed since the export")
    return problems
