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
import sys

import build_rules
import normal_db
import privacy_store
from build_port import NormalBuild
from build_rules import BuildError, NormalBuildError

client_sources_note = build_rules.client_sources_note


class NormalOutputs:
    """What follows a build, over the normal-db builder: `build_port.BuildOutputs`. Privacy rules come from the
    private data dir unless given (a bad rules file raises: fail closed)."""

    def __init__(self, files, locations, rules=None):
        self._files, self._locations, self._rules = files, locations, rules

    def report(self, path):
        con = self._files.open_database(path)
        try:
            existing = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            lines = [f"  {table}: {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}"
                     for table in build_rules.report_tables(existing)]
        finally:
            con.close()
        return lines + [f"  file size: {self._files.size(path) / 1_000_000:.1f} MB"]

    def build_normal(self, full_path, directory):
        rules = self._rules if self._rules is not None else privacy_store.load_rules(self._locations.privacy_rules())
        path, counts = normal_db.build_normal_atomic(self._files, full_path, directory, rules)
        return NormalBuild(path, normal_db.format_counts(counts))

    def discard_normal(self, directory):
        build_rules.remove_db_files(self._files.remove_if_exists, self._files.join(directory, normal_db.NORMAL_DB_NAME))


def compose(locations, clock, files=None):
    """The build use case for these `locations` and `clock`, wired to the real adapters through the container."""
    import container
    if files is None:
        import local_buildfiles
        files = local_buildfiles
    return container.Container(locations, clock).build_use_case(NormalOutputs(files, locations), files=files)


def build(target_path, locations, clock, files, sources=None):
    """Build a fresh knowledge.db at `target_path`: the core schema and steps, plus the optional client sources
    in `sources` (default: the ones these `locations` enable). Raises BuildError(stage)."""
    compose(locations, clock, files).build_database(target_path, sources, print)


def main(locations=None, argv=None, clock=None, files=None, app=None):
    """Entry point. `locations` is read from the private checkout (paths.load()) and `clock` from the environment
    (KNOWLEDGE_FROZEN_NOW, else the system clock) unless the caller gives them; `app` is a composed build use case
    (`compose`) for a caller that wants other adapters."""
    if app is None:
        if locations is None:
            import paths
            locations = paths.load()
        if clock is None:
            import clock as clocks
            clock = clocks.from_environment()
        app = compose(locations, clock, files)
    argv = sys.argv[1:] if argv is None else argv
    try:
        if "--check" in argv:
            app.check(print)
        else:
            app.rebuild(print)
    except NormalBuildError as e:
        if e.published:
            print(f"error: knowledge.db was rebuilt, but the normal-only DB failed: {e.cause}; "
                  f"any previous knowledge-normal.db was removed", file=sys.stderr)
        else:
            print(f"error: normal DB build failed: {e.cause}", file=sys.stderr)
        sys.exit(1)
    except BuildError as e:
        print(f"error: build failed in {e.stage}: {e.cause!r}; live DB left untouched", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
