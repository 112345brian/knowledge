"""Import an export into a NEW database (the counterpart of `export_service`).

Application ring (tach layer `application`): it checks every manifest and every file's hash before it creates
anything, then creates the database through `ImportTarget`, inserts the core tables parents first and then the bulk
tables, and removes the new database if anything fails after it was created. The notes, when asked for, are written
last and only to names that do not exist yet. It never prints or exits.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass
from typing import Dict, List, Mapping

import export_rules as rules
from export_port import ExportFiles, ExportScope, ImportTarget, NotesFiles

CHUNK = 500


@dataclass(frozen=True)
class ImportDeps:
    """What `import_database` reaches the world through."""
    export_files: ExportFiles
    import_target: ImportTarget
    export_scope: ExportScope
    notes_target: NotesFiles


def _load(deps: ImportDeps, part: str, required: bool):
    """(manifest, texts) of a part, verified; None when an optional part was never exported."""
    try:
        manifest = rules.read_manifest(deps.export_files.read, part)
    except rules.MissingPart:
        if required:
            raise
        return None                       # only a part that was never exported is optional; a damaged one is an error
    texts = rules.read_texts(deps.export_files.read, part, manifest)
    problems = rules.drift(manifest, texts)
    if problems:
        raise rules.ExportError(f"the {part} part of the export is damaged: " + "; ".join(problems))
    return manifest, texts


def import_database(deps: ImportDeps, with_bulk: bool = True, with_notes: bool = False) -> Mapping[str, Mapping[str, int]]:
    """Create a new database from the export; with `with_notes`, also copy the notes into the notes target. Returns
    {part: {item: rows}} for what was imported. A part that was never exported is skipped (at least one table part must
    exist). Whatever the manifests list is imported, so a partial export gives a database holding only those tables;
    references to tables it lacks make the integrity check fail, and then nothing is created. Nothing is created when
    the export is altered or from another schema version, or names a table no client source owns; the new database
    is removed again if inserting fails."""
    core = _load(deps, rules.CORE, required=False)
    bulk = _load(deps, rules.BULK, required=False) if with_bulk else None
    notes = _load(deps, rules.NOTES, required=True) if with_notes else None
    if core is None and bulk is None:
        raise rules.ExportError("the export has no table files (no core or bulk manifest)")
    versions = {m[0].user_version for m in (core, bulk) if m}
    if len(versions) != 1:
        raise rules.ExportError("the core and bulk parts are from different schema versions")
    version = versions.pop()
    known = set(deps.export_scope.known_bulk_tables())
    unknown = [t for t in (bulk[0].tables if bulk else ()) if t not in known]
    if unknown:
        raise rules.ExportError("the bulk part has table(s) no client source owns: " + ", ".join(sorted(unknown)))
    order = [t for t in (*rules.CORE_TABLES, *rules.LOG_TABLES) if core and t in core[0].tables]
    parsed: Dict[str, List] = {t: rules.decode_table(core[1][t]) for t in order} if core else {}
    bulk_parsed: Dict[str, List] = {t: rules.decode_table(bulk[1][t]) for t in sorted(bulk[0].tables)} if bulk else {}
    if notes:
        clash = sorted(set(notes[0].tables) & set(deps.notes_target.names()))
        if clash:
            raise FileExistsError("these notes already exist and are never overwritten: " + ", ".join(clash))
    schema = deps.export_scope.schema_files(list(bulk_parsed))
    if any(t in rules.LOG_TABLES for t in parsed):
        schema.append(rules.LIVE_SCHEMA)
    deps.import_target.create(schema)
    try:
        if deps.import_target.user_version() != version:
            raise rules.ExportError(f"the export is schema version {version}, this code builds "
                                    f"{deps.import_target.user_version()}")
        for tables in (parsed, bulk_parsed):
            for table, rows in tables.items():
                for start in range(0, len(rows), CHUNK):
                    deps.import_target.insert(table, rows[start:start + CHUNK])
        found = deps.import_target.finish()
        if found:
            raise rules.ExportError("the imported database is not sound: " + "; ".join(found))
    except BaseException:
        deps.import_target.discard()
        raise
    result: Dict[str, Mapping[str, int]] = {}
    if parsed:
        result[rules.CORE] = {t: len(r) for t, r in parsed.items()}
    if bulk:
        result[rules.BULK] = {t: len(r) for t, r in bulk_parsed.items()}
    if notes:
        for name in sorted(notes[1]):
            deps.notes_target.write(name, notes[1][name])
        result[rules.NOTES] = {n: 1 for n in sorted(notes[1])}
    return result
