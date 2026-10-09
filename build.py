#!/usr/bin/env python3
"""Rebuild ~/MEGA/library/knowledge.db from scratch: schema + every
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
import sqlite3, os, sys, shutil, datetime, importlib.util, tempfile

import build_rules
from paths import KNOWLEDGE_DB_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.expanduser(KNOWLEDGE_DB_DIR)
LIVE_DB = os.path.join(DB_DIR, "knowledge.db")
SCHEMA = os.path.join(HERE, "schema.sql")
BACKUP_DIR = os.path.join(DB_DIR, "backups")
KEEP_BACKUPS = build_rules.KEEP_BACKUPS

STEPS = list(build_rules.STEPS)


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
    import build_info, paths
    build_info.record(con, paths, HERE, paths.PRIVATE_DATA_DIR)
    con.commit()


def build(target_path):
    if os.path.exists(target_path):
        os.remove(target_path)
    con = sqlite3.connect(target_path)
    try:
        con.execute("PRAGMA foreign_keys = ON;")
        try:
            with open(SCHEMA) as f:
                con.executescript(f.read())
        except Exception as e:
            raise BuildError("schema", e) from e
        for step in STEPS:
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
    fd, tmp = tempfile.mkstemp(prefix="knowledge.db.building-", dir=directory)
    os.close(fd)
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(tmp, 0o666 & ~umask)  # mkstemp makes it 0600; keep the mode a plain sqlite3.connect gives
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
    for table in build_rules.REPORT_TABLES:
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
