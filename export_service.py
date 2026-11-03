"""Export the database to deterministic text files and check them against it (`import_service` reads them back).

Application ring (tach layer `application`): it orders the work and names the failures through the ports in
`export_port`; `export_rules` decides the format and which tables a selection means. It never prints or exits.

What is exported is a SELECTION (named tables and groups, minus skipped tables; nothing named means everything the
database has), so a single table can be exported or verified. `export_database` computes every text before it writes
anything, then writes each part's manifest after that part's files, merged with what the folder already lists. Notes
(the citation-note markdown) are a separate switch.
"""

__arbor__ = {"ring": "application"}

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

import export_rules as rules
from export_port import ExportFiles, ExportReads, ExportScope, NotesFiles


@dataclass(frozen=True)
class ExportDeps:
    """What `export_database` and `verify_export` reach the world through."""
    export_reads: ExportReads
    export_files: ExportFiles
    export_scope: ExportScope
    notes_source: NotesFiles


def _selected(deps: ExportDeps, tables, groups, skip) -> List[str]:
    existing = deps.export_reads.tables()
    return rules.select(tables, groups, skip, deps.export_scope.groups(), existing, deps.export_scope.known_bulk_tables())


def _by_part(selected: Sequence[str]) -> Dict[str, List[str]]:
    parts: Dict[str, List[str]] = {}
    for table in selected:
        parts.setdefault(rules.part_of(table), []).append(table)
    return parts


def _note_texts(deps: ExportDeps) -> Dict[str, str]:
    if not deps.notes_source.names():
        raise rules.ExportError("there are no notes to export at the configured notes directory")
    return {rules.check_note(n): deps.notes_source.read(n) for n in deps.notes_source.names()}


def _previous(deps: ExportDeps, part: str):
    """The part's manifest as the folder holds it now, or None when there is none or it cannot be read."""
    try:
        return rules.read_manifest(deps.export_files.read, part)
    except rules.ExportError:
        return None


def _keep_large_parts_out_of_git(deps: ExportDeps) -> None:
    """Give the export folder a `.gitignore` for the parts that stay out of git, unless it already has one (the user's
    own is never overwritten). Without it the first bulk or notes export leaves untracked files in the private repo,
    and `add-fact` refuses to run on a repo that is not clean."""
    try:
        deps.export_files.read(rules.GITIGNORE)
    except FileNotFoundError:
        deps.export_files.write(rules.GITIGNORE, rules.gitignore_text())


def export_database(deps: ExportDeps, tables: Sequence[str] = (), groups: Sequence[str] = (), skip: Sequence[str] = (),
                    include_notes: bool = False) -> Mapping[str, Mapping[str, int]]:
    """Write the selected tables (and the notes, with `include_notes`). Returns {part: {item: rows}} (a note counts 1).
    A table already in the folder but not selected is left alone and stays listed in its part's manifest."""
    selected = _selected(deps, tables, groups, skip)
    if not selected and not include_notes:
        raise rules.ExportError("nothing is selected to export")
    computed: Dict[str, Dict[str, str]] = {
        part: {t: rules.encode_table(deps.export_reads.rows(t)) for t in names} for part, names in _by_part(selected).items()}
    if include_notes:
        computed[rules.NOTES] = _note_texts(deps)         # everything is computed before anything is written
    version = deps.export_reads.user_version()
    result: Dict[str, Mapping[str, int]] = {}
    for part, texts in computed.items():
        for name, text in texts.items():
            deps.export_files.write(rules.note_path(name) if part == rules.NOTES else rules.file_name(part, name), text)
        deps.export_files.write(rules.manifest_name(part), rules.build_manifest(version, texts, _previous(deps, part)))
        if part in rules.UNTRACKED_PARTS:
            _keep_large_parts_out_of_git(deps)
        result[part] = {n: (1 if part == rules.NOTES else rules.line_count(t)) for n, t in texts.items()}
    return result


def verify_export(deps: ExportDeps, tables: Sequence[str] = (), groups: Sequence[str] = (), skip: Sequence[str] = (),
                  include_notes: bool = False) -> List[str]:
    """Where the export files and the database disagree for the selection, as lines (empty: the files are exactly what
    an export of those tables would write now). Writes nothing. A selected table that was never exported is a problem."""
    problems: List[str] = []
    version = deps.export_reads.user_version()
    wanted: Dict[str, List[str]] = _by_part(_selected(deps, tables, groups, skip))
    if include_notes:
        wanted[rules.NOTES] = sorted(_note_texts(deps))
    for part, names in wanted.items():
        try:
            manifest = rules.read_manifest(deps.export_files.read, part)
        except rules.ExportError as e:
            problems.append(str(e))
            continue
        if manifest.user_version != version:
            problems.append(f"{part}: schema version: export {manifest.user_version}, database {version}")
        on_disk = rules.read_texts(deps.export_files.read, part, manifest)
        current = _note_texts(deps) if part == rules.NOTES else {t: rules.encode_table(deps.export_reads.rows(t)) for t in names}
        for name in names:
            if name not in manifest.tables:
                problems.append(f"{part}/{name}: is in the database but not in the export")
                continue
            problems += [f"{part}/{p}" for p in rules.drift(rules.Manifest(manifest.user_version, {name: manifest.tables[name]}), on_disk)]
            if name in on_disk and on_disk[name] != current[name]:
                problems.append(f"{part}/{name}: the database has changed since the export")
    return problems
