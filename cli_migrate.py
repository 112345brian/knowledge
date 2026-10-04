"""Typer command `migrate-memory` (issue #29): a thin printer over migrate_memory.migrate().

Registered from knowledge.py with `cli_migrate.register(app)`. Kept in its own module so the
command and its library stay out of knowledge.py's query code. Does not import knowledge.py.

Exit codes: 0 ok, 1 refused or files with problems reported (the rest is still processed), 3 the
facts ARE written but the commit failed (same as add-fact).
"""
import json
import sys

import typer

import migrate_memory


def _text(report, verbose):
    mode = "dry run: nothing written" if report.dry_run else "migrated"
    print(f"migrate-memory ({mode}); root {report.root}")
    if report.refused:
        print(f"error: {report.refused}", file=sys.stderr)
    for r in report.files:
        warn = f" [{'; '.join(r.warnings)}]" if r.warnings else ""
        if verbose or r.action != "unchanged":
            detail = f": {r.detail}" if r.detail else ""
            print(f"  {r.action:<16} {r.type or '-':<9} {r.project}/{r.file}{detail}{warn}")
    for t, row in sorted(report.counts().items()):
        print(f"  {t}: " + ", ".join(f"{a} {n}" for a, n in sorted(row.items())))
    tot = report.totals()
    print("total: " + ", ".join(f"{a} {n}" for a, n in tot.items() if n) + f"; MEMORY.md index files skipped {report.index_files}")
    if report.not_in_git:
        print("note: the data dir is not inside a git repository; the change was not committed.", file=sys.stderr)
    if report.commit:
        print(f"Committed {report.commit}")
    if report.detached:
        print("warning: the private repo has a detached HEAD; that commit is not on any branch.", file=sys.stderr)
    if report.commit_error:
        print(f"error: the facts ARE written but NOT committed: {report.commit_error}", file=sys.stderr)


def register(app):
    @app.command("migrate-memory",
                 help="Import Claude Code memory (~/.claude/projects/*/memory/*.md) as private pending facts. "
                      "feedback files are skipped; a second run adds nothing. Use --dry-run first.")
    def cmd_migrate_memory(
        memory_root: str = typer.Option(migrate_memory.DEFAULT_ROOT, "--memory-root",
                                        help="Folder holding <project>/memory/*.md (default ~/.claude/projects)."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be written; write nothing."),
        as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON instead of text."),
        allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Skip the clean-tree check on knowledge-private "
                                         "(e.g. after a killed run); the commit still contains only the facts file."),
        verbose: bool = typer.Option(False, "--verbose", help="Also list files already migrated."),
    ):
        report = migrate_memory.migrate(root=memory_root, dry_run=dry_run, allow_dirty=allow_dirty)
        if as_json:
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        else:
            _text(report, verbose)
        raise typer.Exit(report.exit_code)

    return cmd_migrate_memory
