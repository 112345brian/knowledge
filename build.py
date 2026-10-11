#!/usr/bin/env python3
"""Rebuild the configured knowledge database from scratch: schema + every
seed/ingest script in this directory, in order. This is the ONLY sanctioned
way to change knowledge.db's structure or bulk contents -- edit a script
here and rerun, never ALTER/INSERT by hand against the live file.

Usage:
    python3 build.py            # rebuilds knowledge.db (backs up the old one first)
    python3 build.py --check    # builds into a temp file and reports counts, doesn't touch the live DB

After the swap, knowledge-normal.db (#21: only visibility='normal' facts and what hangs off them, see
normal_db.py) is built the same atomic way into the same directory and leak-checked. If that fails the
main build stays in place, a stale knowledge-normal.db is removed, and the exit code is non-zero.

The rebuild goes into a temp file next to the live DB and is os.replace()d onto
it only after every step succeeds, so a failing step (exit 1, naming the step)
leaves the previous knowledge.db byte-identical, and a reader holding the old
file open keeps a consistent snapshot.
"""
import os
import sys

import build_rules
import local_inputs
import local_stores
import private_git
from pipeline_context import StepContext

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP_BACKUPS = build_rules.KEEP_BACKUPS

STEPS = None   # the step files to run, relative to HERE; None = the core steps plus those of the enabled client sources


def schema_sql(files, sources):
    """The schema text for `sources`: schema.sql plus the fragment of each enabled client source."""
    return "\n".join(files.read_text(os.path.join(HERE, name)) for name in build_rules.schema_files(sources))


def client_sources_note(sources, implicit=False):
    """One line saying which optional client sources this build includes (and why, if that was a guess)."""
    if not sources:
        return "[build] client sources: none (core only; list them in CLIENT_SOURCES in local_paths.py)"
    note = f"[build] client sources: {', '.join(sources)}"
    if implicit:
        note += ("  (assumed: local_paths.py defines the music inputs but no CLIENT_SOURCES; add "
                 f"CLIENT_SOURCES = {tuple(sources)!r} to make this explicit)")
    return note


class BuildError(Exception):
    """A build stage failed; `stage` names it (a step filename or 'schema')."""
    def __init__(self, stage, cause):
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause


def record_build_info(con, locations, clock):
    """#47: which inputs and code produced this db (build_info / build_inputs)."""
    import build_info_store
    build_info_store.record(con, locations, HERE, private_git, clock)
    con.commit()


def build(target_path, locations, clock, files, sources=None):
    """Build a fresh knowledge.db at `target_path`: the core schema and steps, plus the optional client sources
    in `sources` (default: the ones these `locations` enable)."""
    sources = build_rules.check_client_sources(locations.client_sources if sources is None else sources)
    print(client_sources_note(sources, implicit=locations.client_sources_implicit and sources == locations.client_sources))
    configured = [name for name, value in (("CONCERTS_CSV", locations.concerts_csv), ("RYM_EXPORT_CSV", locations.rym_export_csv),
                                           ("SCROBBLES_JSON", locations.scrobbles_json)) if value]
    missing = build_rules.missing_paths(sources, configured)
    if missing:
        raise BuildError("config", "client source(s) enabled without their input file: " +
                         ", ".join(f"{src} needs {name}" for src, name in missing) + " (set it in local_paths.py)")
    files.remove_if_exists(target_path)
    con = files.open_database(target_path)
    try:
        con.execute("PRAGMA foreign_keys = ON;")
        try:
            con.executescript(schema_sql(files, sources))
        except Exception as e:
            raise BuildError("schema", e) from e
        for step in (build_rules.steps_for(sources) if STEPS is None else STEPS):
            try:
                mod = files.load_module(os.path.join(HERE, step))
                mod.run(con, StepContext(locations, local_inputs, local_stores))
            except Exception as e:
                raise BuildError(step, e) from e
        try:
            record_build_info(con, locations, clock)
        except Exception as e:
            raise BuildError("build_info", e) from e
        con.execute("VACUUM;")
        con.execute("ANALYZE;")
    finally:
        con.close()


def build_to_temp(directory, locations, clock, files):
    """Build into a fresh unique temp file in `directory`; return its path.
    On failure the temp file is removed and BuildError propagates."""
    tmp = os.path.join(directory, f"knowledge.db.building-{files.unique_token()}")
    files.create_exclusive(tmp)
    try:
        build(tmp, locations, clock, files)
    except BaseException:
        remove_db_files(files, tmp)
        raise
    return tmp


def remove_db_files(files, path):
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        files.remove_if_exists(p)


def prune_backups(files, backup_dir, keep=KEEP_BACKUPS):
    """Keep only the `keep` most recent backups (see build_rules.backups_to_prune)."""
    for old in build_rules.backups_to_prune(files.listdir(backup_dir), keep):
        files.remove(os.path.join(backup_dir, old))
        print(f"  pruned old backup {old}")


def build_normal(files, full_path, directory, locations, rules=None):
    """Build + leak-check knowledge-normal.db in `directory` from the freshly built `full_path`.
    Privacy rules come from the private data dir (a bad rules file raises: fail closed)."""
    import normal_db
    if rules is None:
        import privacy_store
        rules = privacy_store.load_rules(locations.privacy_rules())
    path, counts = normal_db.build_normal_atomic(files, full_path, directory, rules)
    print(f"Built {path} (leak test passed)")
    print(normal_db.format_counts(counts))
    return path


def report(files, path):
    con = files.open_database(path)
    cur = con.cursor()
    existing = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in build_rules.report_tables(existing):
        n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n}")
    con.close()
    print(f"  file size: {files.size(path) / 1_000_000:.1f} MB")


def main(locations=None, argv=None, clock=None, files=None):
    """Entry point. `locations` is read from the private checkout (paths.load()) and `clock` from the environment
    (KNOWLEDGE_FROZEN_NOW, else the system clock) unless the caller gives them."""
    if locations is None:
        import paths
        locations = paths.load()
    if clock is None:
        import clock as clocks
        clock = clocks.from_environment()
    if files is None:
        import local_buildfiles
        files = local_buildfiles
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    live_db, db_dir, backup_dir = locations.db_path, locations.db_dir, locations.backup_dir
    try:
        if check_only:
            print("Building into a temp file (live DB untouched)...")
            tmp = build_to_temp(files.system_temp_dir(), locations, clock, files)
            try:
                report(files, tmp)
                with files.temporary_directory() as nd:
                    print("Normal-only DB (temp):")
                    build_normal(files, tmp, nd, locations)
            except Exception as e:
                print(f"error: normal DB build failed: {e}", file=sys.stderr)
                sys.exit(1)
            finally:
                remove_db_files(files, tmp)
            return

        files.makedirs(db_dir)
        print(f"Rebuilding {live_db}...")
        tmp = build_to_temp(db_dir, locations, clock, files)
    except BuildError as e:
        print(f"error: build failed in {e.stage}: {e.cause!r}; live DB left untouched", file=sys.stderr)
        sys.exit(1)

    try:
        if files.exists(live_db):
            files.makedirs(backup_dir)
            backup = os.path.join(backup_dir, build_rules.backup_name(clock.now()))
            files.copy(live_db, backup)
            print(f"Backed up existing DB to {backup}")
            prune_backups(files, backup_dir)
        files.replace(tmp, live_db)
    except BaseException:
        remove_db_files(files, tmp)
        raise
    report(files, live_db)

    # The main build is done and stays in place whatever happens here.
    try:
        build_normal(files, live_db, db_dir, locations)
    except Exception as e:
        print(f"error: knowledge.db was rebuilt, but the normal-only DB failed: {e}; "
              f"any previous knowledge-normal.db was removed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
