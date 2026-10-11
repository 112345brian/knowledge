"""Building the normal-tier database and checking it for leaks: the use case. It reads the full DB's rows and
writes the normal DB through the port protocols (`normal_db_port`, `build_files_port`) and applies the rules in
`normal_rules` and `leak_rules` (it imports no adapter). `normal_db` is the entry point that binds it to the
SQLite adapters.

The lifecycle (`build_normal_atomic`): build into a temporary file beside the target, leak-check it against the
full DB, then replace the target. On ANY failure the temporary file is removed AND a previous normal DB is
removed too: a stale normal DB could still hold a fact that has since been made private, so no file is safer
than an out-of-date one.
"""
from dataclasses import dataclass

from build_files_port import BuildFiles
from leak_rules import check_markers, find_leaks, format_leaks, markers_from
from normal_db_port import LeakScan, NormalSink, NormalSource
from normal_rules import NORMAL_DB_NAME, NormalDbError, select_content, verify_integrity


@dataclass(frozen=True)
class NormalDbDeps:
    """Everything this service reaches the disk and the databases through; the entry point supplies each one."""
    files: BuildFiles
    source: NormalSource
    sink: NormalSink
    leak_scan: LeakScan


def scan(deps, normal_path, markers):
    """markers: iterable of strings (or {label: string}). Returns [(label, marker, where)]. Raises ValueError on
    an empty marker, before anything is read."""
    markers = check_markers(markers)
    raw = deps.files.read_bytes(normal_path)
    return find_leaks(raw, deps.leak_scan.cells(normal_path), markers)


def derive_markers(deps, full_path):
    """{label: text} for everything in the full DB that must never reach the normal tier."""
    return markers_from(deps.leak_scan.private_material(full_path))


def scan_against_full(deps, normal_path, full_path):
    return scan(deps, normal_path, derive_markers(deps, full_path))


def build_normal_db(deps, full_path, target_path, rules):
    """Write a fresh normal DB at exactly `target_path` (must not exist). `rules` is a privacy.Rules (pass
    privacy.Rules() for none). Returns {table: row count}."""
    if deps.files.exists(target_path):
        raise NormalDbError(f"{target_path} already exists; build into a fresh path")
    content = select_content(deps.source.read_rows(full_path), rules)
    deps.sink.write(target_path, content)
    integrity = deps.sink.inspect(target_path)
    verify_integrity(integrity)
    return integrity.counts


def remove_db_files(files, path):
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        files.remove_if_exists(p)


def build_normal_atomic(deps, full_path, directory, rules, leak_check=True):
    """Build into a temp file in `directory`, leak-check it against the full DB, then replace
    knowledge-normal.db. On any failure the temp file and a previous knowledge-normal.db are removed.
    Returns (final_path, counts)."""
    files = deps.files
    final = files.join(directory, NORMAL_DB_NAME)
    tmp = files.join(directory, f"{NORMAL_DB_NAME}.building-{files.unique_token()}")
    files.create_exclusive(tmp)     # fails if the directory is unwritable, before any work is done
    files.remove_if_exists(tmp)     # build_normal_db expects a fresh path
    try:
        counts = build_normal_db(deps, full_path, tmp, rules)
        if leak_check:
            leaks = scan_against_full(deps, tmp, full_path)
            if leaks:
                raise NormalDbError("leak test failed:\n" + format_leaks(leaks))
        files.replace(tmp, final)
    except BaseException:
        remove_db_files(files, tmp)
        remove_db_files(files, final)
        raise
    return final, counts
