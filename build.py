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
import sqlite3, os, sys, shutil, datetime, importlib.util, tempfile, uuid

import build_rules
import private_git
from paths import CLIENT_SOURCES, CLIENT_SOURCES_IMPLICIT, KNOWLEDGE_DB_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.expanduser(KNOWLEDGE_DB_DIR)
LIVE_DB = os.path.join(DB_DIR, "knowledge.db")
BACKUP_DIR = os.path.join(DB_DIR, "backups")
KEEP_BACKUPS = build_rules.KEEP_BACKUPS

STEPS = None   # the step files to run, relative to HERE; None = the core steps plus those of the enabled client sources


def schema_sql(sources=None):
    """The schema text for `sources` (default: this checkout's CLIENT_SOURCES): schema.sql plus the fragment of
    each enabled client source."""
    sources = CLIENT_SOURCES if sources is None else sources
    parts = []
    for name in build_rules.schema_files(sources):
        with open(os.path.join(HERE, name)) as f:
            parts.append(f.read())
    return "\n".join(parts)


def client_sources_note(sources, implicit=False):
    """One line saying which optional client sources this build includes (and why, if that was a guess)."""
    if not sources:
        return "[build] client sources: none (core only; list them in CLIENT_SOURCES in local_paths.py)"
    note = f"[build] client sources: {', '.join(sources)}"
    if implicit:
        note += ("  (assumed: local_paths.py defines the music inputs but no CLIENT_SOURCES; add "
                 f"CLIENT_SOURCES = {tuple(sources)!r} to make this explicit)")
    return note


def load_module(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuildError(Exception):
    """A build stage failed; `stage` names it (a step filename or 'schema')."""
    def __init__(self, stage, cause):
        super().__init__(f"{stage}: {cause}")
        self.stage = stage
        self.cause = cause


def record_build_info(con):
    """#47: which inputs and code produced this db (build_info / build_inputs)."""
    import build_info_store, paths
    build_info_store.record(con, paths, HERE, paths.PRIVATE_DATA_DIR, private_git)
    con.commit()


def build(target_path, sources=None):
    """Build a fresh knowledge.db at `target_path`: the core schema and steps, plus the optional client sources
    in `sources` (default: this checkout's CLIENT_SOURCES)."""
    sources = build_rules.check_client_sources(CLIENT_SOURCES if sources is None else sources)
    print(client_sources_note(sources, implicit=CLIENT_SOURCES_IMPLICIT and sources == CLIENT_SOURCES))
    import paths
    missing = build_rules.missing_paths(sources, [n for n in ("CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON") if getattr(paths, n, None)])
    if missing:
        raise BuildError("config", "client source(s) enabled without their input file: " +
                         ", ".join(f"{src} needs {name}" for src, name in missing) + " (set it in local_paths.py)")
    if os.path.exists(target_path):
        os.remove(target_path)
    con = sqlite3.connect(target_path)
    try:
        con.execute("PRAGMA foreign_keys = ON;")
        try:
            con.executescript(schema_sql(sources))
        except Exception as e:
            raise BuildError("schema", e) from e
        for step in (build_rules.steps_for(sources) if STEPS is None else STEPS):
            try:
                mod = load_module(os.path.join(HERE, step))
                mod.run(con)
            except Exception as e:
                raise BuildError(step, e) from e
        try:
            record_build_info(con)
        except Exception as e:
            raise BuildError("build_info", e) from e
        con.execute("VACUUM;")
        con.execute("ANALYZE;")
    finally:
        con.close()


def build_to_temp(directory):
    """Build into a fresh unique temp file in `directory`; return its path.
    On failure the temp file is removed and BuildError propagates."""
    tmp = os.path.join(directory, f"knowledge.db.building-{uuid.uuid4().hex}")
    # Let the kernel apply umask at creation; reading it with os.umask(0) changes the
    # process-wide setting while other threads may be creating files.
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    os.close(fd)
    try:
        build(tmp)
    except BaseException:
        remove_db_files(tmp)
        raise
    return tmp


def remove_db_files(path):
    for p in (path, path + "-journal", path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def prune_backups(keep=KEEP_BACKUPS):
    """Keep only the `keep` most recent backups (see build_rules.backups_to_prune)."""
    for old in build_rules.backups_to_prune(os.listdir(BACKUP_DIR), keep):
        os.remove(os.path.join(BACKUP_DIR, old))
        print(f"  pruned old backup {old}")


def build_normal(full_path, directory, rules=None):
    """Build + leak-check knowledge-normal.db in `directory` from the freshly built `full_path`.
    Privacy rules come from the private data dir (a bad rules file raises: fail closed)."""
    import normal_db
    if rules is None:
        import privacy_store
        rules = privacy_store.load_rules()
    path, counts = normal_db.build_normal_atomic(full_path, directory, rules)
    print(f"Built {path} (leak test passed)")
    print(normal_db.format_counts(counts))
    return path


def report(path):
    con = sqlite3.connect(path)
    cur = con.cursor()
    existing = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in build_rules.report_tables(existing):
        n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n}")
    con.close()
    print(f"  file size: {os.path.getsize(path) / 1_000_000:.1f} MB")


def main():
    check_only = "--check" in sys.argv
    try:
        if check_only:
            print("Building into a temp file (live DB untouched)...")
            tmp = build_to_temp(tempfile.gettempdir())
            try:
                report(tmp)
                with tempfile.TemporaryDirectory() as nd:
                    print("Normal-only DB (temp):")
                    build_normal(tmp, nd)
            except Exception as e:
                print(f"error: normal DB build failed: {e}", file=sys.stderr)
                sys.exit(1)
            finally:
                remove_db_files(tmp)
            return

        os.makedirs(DB_DIR, exist_ok=True)
        print(f"Rebuilding {LIVE_DB}...")
        tmp = build_to_temp(DB_DIR)
    except BuildError as e:
        print(f"error: build failed in {e.stage}: {e.cause!r}; live DB left untouched", file=sys.stderr)
        sys.exit(1)

    try:
        if os.path.exists(LIVE_DB):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            backup = os.path.join(BACKUP_DIR, build_rules.backup_name(datetime.datetime.now()))
            shutil.copy2(LIVE_DB, backup)
            print(f"Backed up existing DB to {backup}")
            prune_backups()
        os.replace(tmp, LIVE_DB)
    except BaseException:
        remove_db_files(tmp)
        raise
    report(LIVE_DB)

    # The main build is done and stays in place whatever happens here.
    try:
        build_normal(LIVE_DB, DB_DIR)
    except Exception as e:
        print(f"error: knowledge.db was rebuilt, but the normal-only DB failed: {e}; "
              f"any previous knowledge-normal.db was removed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
