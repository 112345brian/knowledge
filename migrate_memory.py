"""Import Claude Code memory files as private, pending facts (issue #29).

Library only: nothing here prints or exits (the Typer command lives in cli_migrate.py). This module is
the facade: it binds the use case in `migrate_memory_service` to the real adapters and keeps the public API.

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
import add_fact
import clock
import migrate_memory_service
import migrate_memory_store
import paths
import private_git
from wiring import bind

DEPS = migrate_memory_service.MigrateMemoryDeps(git=private_git, clock=clock, memory=migrate_memory_store, add_fact=add_fact,
                                            defaults=paths.Defaults())

migrate = bind(migrate_memory_service.migrate, DEPS)
