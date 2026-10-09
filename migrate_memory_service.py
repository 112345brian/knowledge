"""Import Claude Code memory files as private, pending facts (issue #29).

Library only: nothing here prints or exits (the Typer command lives in cli_migrate.py). This module is
the use case (through the ports in `ports`, no adapter imported); `migrate_memory` is the facade that
binds it to the real adapters and keeps the public API.

Source: `<root>/*/memory/*.md`, root defaulting to ~/.claude/projects. Index files named
`MEMORY.md` are skipped and counted. Symlinked files and symlinked project folders are never
followed (a link could point anywhere): they are reported as `skipped-symlink`.

Frontmatter: `name`, `description` and `type` / `originSessionId` / `modified`, each found either
nested under `metadata:` or at the top level. A tiny YAML subset is parsed (scalars, one level of
nesting, `>`/`|` block scalars); no YAML dependency.

Mapping (documented defaults, nothing beyond them is assigned):
  * type `project` / `user` / `reference` -> one pending fact per file via add_fact.append_fact, so
    validation, the locked atomic append, the privacy resolver and provenance are reused:
      subject       claude-memory-<type>     (kebab-case; one subject per type, no per-project
                                              subjects, which would bake personal folder names
                                              into the subject tree)
      trust_level   unverified               (a memory file is a note Claude wrote, not a source)
      status        pending, visibility private (the resolver may only raise it), is_personal true
      statement     the body with whitespace collapsed; if longer than STATEMENT_MAX chars it is cut
                    at a word boundary and ends in " [...]". Nothing is lost: when cut, `notes`
                    holds the full body. An empty body falls back to `description`.
      source_quote  the first QUOTE_LINES non-empty body lines, capped at QUOTE_MAX chars
      notes         provenance (project folder / file name, name, description), the content hash
                    marker used for "changed since migration", and the full body when truncated
      captured_via  migrate-memory, session_id = originSessionId when present
      recheck_by    `modified` date + RECHECK_DAYS (180) as YYYY-MM-DD; with no usable `modified`,
                    today + RECHECK_DAYS and a warning on the file
  * type `feedback` -> skipped and listed (instructions to Claude, not facts)
  * any other / missing type, no or broken frontmatter, unreadable, non-UTF-8, NUL bytes, empty,
    or larger than MAX_FILE_BYTES (refused, never truncated) -> a `problem` row, never a crash.

Idempotence: source_key = "mm-" + first 16 hex of sha1("<project folder>/<file name>"), so the
identity is the file's place, not its content. A key already present in any entry file means the
file is `unchanged`, or `changed` (reported, NOT re-added) when its sha256 differs from the one
stored in the entry's notes. A run is serialized against other migrate runs with a lock file in
the system temp dir (keyed by the data file). It cannot be held across append_fact's own lock,
so a concurrent add_fact still just appends its own random-keyed entry.
"""
import contextlib

import revisions
from migrate_memory_rules import FileResult, MigrationReport, plan_file

DEFAULT_ROOT = "~/.claude/projects"


def migrate(ports, root=None, data_path=None, db_path=None, dry_run=False, allow_dirty=False, today=None):
    """Plan and (unless dry_run) perform the import. Returns a MigrationReport; never prints or
    exits. Writes at most `data_path` and one commit of exactly that file."""
    root = DEFAULT_ROOT if root is None else root
    data_path = ports.defaults.data_path if data_path is None else data_path
    db_path = ports.defaults.db_path if db_path is None else db_path
    report = MigrationReport(root=ports.memory.absolute_root(root), dry_run=dry_run)
    if not ports.memory.root_exists(report.root):
        report.refused = f"memory root {report.root} is not a directory"
        return report
    found, report.index_files, links = ports.memory.discover(root)
    for project, name, why in links:
        report.files.append(FileResult(project, name, None, "skipped-symlink", why))

    lock = contextlib.nullcontext() if dry_run else ports.memory.run_lock(data_path)
    with lock:
        try:
            existing = ports.memory.existing_entries(ports.memory.data_dir_of(data_path))
        except (revisions.RevisionError, OSError) as e:
            report.refused = f"cannot read the existing facts: {e}"
            return report
        todo = []   # (FileResult, NewFact)
        seen_keys = {}
        today = today or ports.clock.now().date()
        for project, name, path in found:
            mf = ports.memory.read_memory_file(path, project, name)
            res, fact, truncated = plan_file(mf, project, name, existing, seen_keys, today)
            report.files.append(res)
            if fact is None:
                continue
            errors, _ = ports.add_fact.validate_fact(fact, db_path)
            if errors:
                res.detail = "; ".join(errors)
                continue
            res.action = "would-add"
            res.detail = "statement cut, full text in notes" if truncated else ""
            todo.append((res, fact))

        if dry_run or not todo:
            return report

        repo = None
        try:
            repo = ports.git.find_repo(ports.memory.data_dir_of(data_path))
            if repo is None:
                report.not_in_git = True
            elif not allow_dirty:
                ports.git.ensure_clean_tree(repo)
        except ports.git.PrivateGitError as e:
            report.refused = str(e)
            for res, _ in todo:
                res.action, res.detail = "problem", "not written: run refused"
            return report

        written = 0
        try:
            for res, fact in todo:
                result = ports.add_fact.append_fact(fact, data_path=data_path, db_path=db_path)
                if not result.ok:
                    res.action, res.detail = "problem", "; ".join(result.errors)
                    continue
                res.action = "added"
                written += 1
        finally:
            if written and repo is not None:
                message = f"migrate-memory: {written} pending fact{'s' if written != 1 else ''}"
                try:
                    report.commit = ports.git.commit_private_change([data_path], message, repo)
                    report.detached = ports.git.is_detached(repo)
                except ports.git.PrivateGitError as e:
                    report.commit_error = str(e)
    return report
