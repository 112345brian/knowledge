"""Import an export into a NEW database (the counterpart of `export_service`).

Application ring (tach layer `application`): it checks the manifest and every file's hash before it creates anything,
then creates the database through `ImportTarget`, inserts tables parents first, and removes the new database if
anything fails after it was created. It never prints or exits.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass
from typing import Mapping

import export_rules as rules
from export_port import ExportFiles, ImportTarget

CHUNK = 500


@dataclass(frozen=True)
class ImportDeps:
    """What `import_database` reaches the world through."""
    export_files: ExportFiles
    import_target: ImportTarget


def import_database(deps: ImportDeps) -> Mapping[str, int]:
    """Create a new database from the export. Returns {table: rows}. Nothing is created when the export is
    incomplete, altered or from another schema version; the new database is removed again if inserting fails."""
    manifest = rules.read_manifest(deps.export_files.read)
    texts = rules.read_texts(deps.export_files.read)
    problems = rules.drift(manifest, texts)
    if problems:
        raise rules.ExportError("the export is damaged: " + "; ".join(problems))
    parsed = {t: rules.decode_table(texts[t]) for t in rules.EXPORT_TABLES}
    deps.import_target.create()
    try:
        if deps.import_target.user_version() != manifest.user_version:
            raise rules.ExportError(f"the export is schema version {manifest.user_version}, this code builds "
                                    f"{deps.import_target.user_version()}")
        for table in rules.EXPORT_TABLES:
            rows = parsed[table]
            for start in range(0, len(rows), CHUNK):
                deps.import_target.insert(table, rows[start:start + CHUNK])
        found = deps.import_target.finish()
        if found:
            raise rules.ExportError("the imported database is not sound: " + "; ".join(found))
    except BaseException:
        deps.import_target.discard()
        raise
    return {t: len(parsed[t]) for t in rules.EXPORT_TABLES}
