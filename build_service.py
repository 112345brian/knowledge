"""Rebuild the knowledge database: the use case behind `build.py` and `knowledge build`.

It owns the ORDER (schema, each step in registry order, provenance, finalize), the FAILURE NAMING (every stage
that can fail raises `BuildError(stage)`), the ATOMIC PUBLICATION (a full build in a temp file next to the live
database, then one replace: a failure anywhere before it leaves the previous knowledge.db byte-identical), the
BACKUP LIFECYCLE (a unique name per backup, never overwritten, only the newest `KEEP_BACKUPS` kept) and the
NORMAL-DB GENERATION after publication (a failure there keeps the new knowledge.db and removes the old
knowledge-normal.db, which could hold a fact that has since become private).

It never prints or exits: progress goes to the `emit` callable the driving adapter passes, and the outcome is
returned. Everything it touches is a port: files (`build_files_port`), a session and step registry, provenance
and outputs (`build_port`), the clock and the locations.
"""
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

import build_rules
from build_files_port import BuildFiles
from build_port import BuildOutputs, NormalBuild, ProvenanceReader, SessionFactory, StepRegistry, StepResult
from build_rules import BuildError, NormalBuildError
from clock_port import Clock
from locations import Locations

BUILDING_PREFIX = "knowledge.db.building-"
PARTIAL_BACKUP_PREFIX = "backup.partial-"    # does not start with BACKUP_PREFIX, so it is never counted or pruned as a backup
MAX_BACKUP_ATTEMPTS = 10_000
DB_SIDECARS = ("", "-journal", "-wal", "-shm")

Emit = Callable[[str], None]


@dataclass(frozen=True)
class BuildDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    files: BuildFiles
    clock: Clock
    locations: Locations
    sessions: SessionFactory
    steps: StepRegistry
    provenance: ProvenanceReader
    outputs: BuildOutputs


@dataclass(frozen=True)
class BuildOutcome:
    """What a rebuild or check did. `live_path` is None for a check; `backup_path` None when there was no
    previous database; `normal` is the normal-only database that was built."""
    live_path: Optional[str]
    backup_path: Optional[str]
    pruned: Tuple[str, ...]
    report: Tuple[str, ...]
    normal: Optional[NormalBuild]
    steps: Tuple[StepResult, ...]


def _quiet(message: str) -> None:
    pass


def remove_db_files(files, path) -> None:
    """`path` and its journal/wal/shm sidecars, whichever exist."""
    for suffix in DB_SIDECARS:
        files.remove_if_exists(path + suffix)


def build_database(deps, target_path, sources=None, emit: Emit = _quiet) -> Tuple[StepResult, ...]:
    """Build a fresh database at `target_path`: the core schema and steps plus the client `sources` (default: the
    ones these locations enable). A failing stage raises BuildError(stage); the file at `target_path` is then
    incomplete and the caller removes it (`build_to_temp` does)."""
    locations = deps.locations
    sources = build_rules.check_client_sources(locations.client_sources if sources is None else sources)
    emit(build_rules.client_sources_note(sources, implicit=locations.client_sources_implicit and sources == locations.client_sources))
    configured = [name for name, value in (("CONCERTS_CSV", locations.concerts_csv), ("RYM_EXPORT_CSV", locations.rym_export_csv),
                                           ("SCROBBLES_JSON", locations.scrobbles_json)) if value]
    missing = build_rules.missing_paths(sources, configured)
    if missing:
        raise BuildError("config", "client source(s) enabled without their input file: " +
                         ", ".join(f"{src} needs {name}" for src, name in missing) + " (set it in local_paths.py)")
    deps.files.remove_if_exists(target_path)
    try:
        session = deps.sessions.open(target_path)
    except Exception as e:
        raise BuildError("open", e) from e
    results = []
    try:
        try:
            session.apply_schema(sources)
        except Exception as e:
            raise BuildError("schema", e) from e
        for step in deps.steps.order(sources):
            try:
                results.append(deps.steps.run(step, session))
            except Exception as e:
                raise BuildError(step, e) from e
        try:
            built_at = deps.clock.now_iso()
            provenance = deps.provenance.read(session.con)
            deps.provenance.write(session.con, provenance, built_at, deps.clock.now_iso())
            session.commit()
        except Exception as e:
            raise BuildError("build_info", e) from e
        try:
            session.optimize()
        except Exception as e:
            raise BuildError("finalize", e) from e
    finally:
        session.close()
    return tuple(results)


def build_to_temp(deps, directory, emit: Emit = _quiet) -> Tuple[str, Tuple[StepResult, ...]]:
    """Build into a fresh, uniquely named temp file in `directory`; return (its path, the step results). On any
    failure the temp file and its sidecars are removed and the error propagates."""
    tmp = deps.files.join(directory, BUILDING_PREFIX + deps.files.unique_token())
    deps.files.create_exclusive(tmp)
    try:
        return tmp, build_database(deps, tmp, emit=emit)
    except BaseException:
        remove_db_files(deps.files, tmp)
        raise


def _claim_backup_path(files, backup_dir, name) -> str:
    """A path in `backup_dir` that did not exist, claimed (created empty) so another build cannot take it too."""
    first = build_rules.next_backup_attempt(name, files.listdir(backup_dir))
    for attempt in range(first, first + MAX_BACKUP_ATTEMPTS):
        path = files.join(backup_dir, build_rules.backup_candidate(name, attempt))
        try:
            files.create_exclusive(path)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free backup name for {name} after {MAX_BACKUP_ATTEMPTS} attempts")


def _backup(files, live_path, backup_dir, name) -> str:
    """Copy `live_path` to a new backup in `backup_dir`. The copy is made under another name and replaced onto
    the claimed one, so a backup with a real name is always complete; a failure leaves no empty or partial file
    behind (it would count as the newest backup and push a real one out)."""
    files.makedirs(backup_dir)
    path = _claim_backup_path(files, backup_dir, name)
    partial = files.join(backup_dir, PARTIAL_BACKUP_PREFIX + files.unique_token())
    try:
        files.copy(live_path, partial)
        files.replace(partial, path)
    except BaseException:
        files.remove_if_exists(partial)
        files.remove_if_exists(path)
        raise
    return path


def _prune(files, backup_dir, emit) -> Tuple[str, ...]:
    """Delete all but the newest backups. Housekeeping after publication: a failure here is a warning, never a
    failed build."""
    removed = []
    try:
        for old in build_rules.backups_to_prune(files.listdir(backup_dir)):
            files.remove(files.join(backup_dir, old))
            removed.append(old)
            emit(f"  pruned old backup {old}")
    except OSError as e:
        emit(f"warning: could not prune old backups: {e}")
    return tuple(removed)


def _publish(deps, tmp, emit) -> Tuple[Optional[str], Tuple[str, ...]]:
    """Swap the finished `tmp` in as the live database: back up the previous one, then one replace. Returns
    (backup path or None, pruned names). A failure removes `tmp` and leaves the live file as it was."""
    files, live, backup_dir = deps.files, deps.locations.db_path, deps.locations.backup_dir
    backup = None
    try:
        if files.exists(live):
            try:
                backup = _backup(files, live, backup_dir, build_rules.backup_name(deps.clock.now()))
            except Exception as e:
                raise BuildError("backup", e) from e
            emit(f"Backed up existing DB to {backup}")
        try:
            files.replace(tmp, live)
        except Exception as e:
            raise BuildError("publish", e) from e
    except BaseException:
        remove_db_files(files, tmp)
        raise
    return backup, (_prune(files, backup_dir, emit) if backup else ())


def rebuild(deps, emit: Emit = _quiet) -> BuildOutcome:
    """Rebuild knowledge.db in place (see the module docstring). Raises BuildError for a failure before the swap
    (the previous knowledge.db is untouched) and NormalBuildError for one after it (the new knowledge.db stays,
    knowledge-normal.db is gone)."""
    files, locations = deps.files, deps.locations
    files.makedirs(locations.db_dir)
    emit(f"Rebuilding {locations.db_path}...")
    tmp, steps = build_to_temp(deps, locations.db_dir, emit)
    backup, pruned = _publish(deps, tmp, emit)
    try:
        report = tuple(deps.outputs.report(locations.db_path))
    except Exception:
        report = ()    # the report is a courtesy: the database is in place and still gets its normal build
    for line in report:
        emit(line)
    # The main build is done and stays in place whatever happens here.
    try:
        normal = deps.outputs.build_normal(locations.db_path, locations.db_dir)
    except BaseException as e:
        deps.outputs.discard_normal(locations.db_dir)
        if isinstance(e, Exception):
            raise NormalBuildError(e, published=True) from e
        raise
    emit(f"Built {normal.path} (leak test passed)")
    emit(normal.summary)
    return BuildOutcome(locations.db_path, backup, pruned, report, normal, steps)


def check(deps, emit: Emit = _quiet) -> BuildOutcome:
    """Build into a throwaway file and a throwaway normal db, report, and touch nothing that exists: the live
    database, its backups and its directory are never read or written."""
    files = deps.files
    emit("Building into a temp file (live DB untouched)...")
    tmp, steps = build_to_temp(deps, files.system_temp_dir(), emit)
    try:
        try:
            report = tuple(deps.outputs.report(tmp))
            for line in report:
                emit(line)
            with files.temporary_directory() as directory:
                emit("Normal-only DB (temp):")
                normal = deps.outputs.build_normal(tmp, directory)
                emit(f"Built {normal.path} (leak test passed)")
                emit(normal.summary)
        except Exception as e:
            raise NormalBuildError(e, published=False) from e
    finally:
        remove_db_files(files, tmp)
    return BuildOutcome(None, None, (), report, normal, steps)
