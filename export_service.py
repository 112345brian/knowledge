"""Export the curated database to deterministic text files and check them against it (`import_service` reads them back).

Application ring (tach layer `application`): it orders the work and names the failures through the ports in
`export_port`; `export_rules` decides the format. It never prints or exits.

`export_database` computes every table's text before it writes anything and writes the manifest last, so a failure
leaves either the old export or files the old manifest still describes.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass
from typing import Dict, List, Mapping

import export_rules as rules
from export_port import ExportFiles, ExportReads

@dataclass(frozen=True)
class ExportDeps:
    """What `export_database` and `verify_export` reach the world through."""
    export_reads: ExportReads
    export_files: ExportFiles


def _texts(reads: ExportReads) -> Dict[str, str]:
    missing = [t for t in rules.EXPORT_TABLES if t not in reads.tables()]
    if missing:
        raise rules.ExportError("the database lacks exported table(s): " + ", ".join(missing))
    return {t: rules.encode_table(reads.rows(t)) for t in rules.EXPORT_TABLES}


def export_database(deps: ExportDeps) -> Mapping[str, int]:
    """Write one file per exported table and the manifest. Returns {table: rows}."""
    texts = _texts(deps.export_reads)
    manifest = rules.build_manifest(deps.export_reads.user_version(), texts)
    for table, text in texts.items():
        deps.export_files.write(rules.file_name(table), text)
    deps.export_files.write(rules.MANIFEST_NAME, manifest)
    return {t: text.count("\n") for t, text in texts.items()}


def verify_export(deps: ExportDeps) -> List[str]:
    """Where the export files and the database disagree, as lines (empty: the files are exactly what an export of the
    database would write now). Writes nothing."""
    problems: List[str] = []
    texts = _texts(deps.export_reads)
    try:
        manifest = rules.read_manifest(deps.export_files.read)
    except rules.ExportError as e:
        return [str(e)]
    if manifest.user_version != deps.export_reads.user_version():
        problems.append(f"schema version: export {manifest.user_version}, database {deps.export_reads.user_version()}")
    on_disk = rules.read_texts(deps.export_files.read)
    problems += rules.drift(manifest, on_disk)
    for table in rules.EXPORT_TABLES:
        if table in on_disk and on_disk[table] != texts[table]:
            problems.append(f"{table}: the database has changed since the export")
    return problems
