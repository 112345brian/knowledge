# Knowledge

Knowledge builds a local SQLite knowledge base from source records and reviewed facts. It provides
tools for ingesting data, tracking revisions, checking provenance, and querying the resulting
database. The database is a build artifact; edit its inputs or code, then rebuild it.

## Data boundary

This repository contains application code, schemas, and synthetic test fixtures. It must not contain
real facts, private configuration, local file paths, or client data. Keep those in a separate private
data directory. `local_paths.py` belongs there and supplies the database location, input paths, and
enabled optional sources. Set `KNOWLEDGE_PRIVATE_DIR` when the private checkout is not next to this
repository.

The build reads fact files, source records, revision logs, privacy rules, subject data, and optional
source inputs from that private directory. Do not put their contents in source files, documentation,
examples, snapshots, or test goldens. Tests should construct synthetic data in temporary directories.

## Install and run

Requires Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
export KNOWLEDGE_PRIVATE_DIR=/path/to/private-data
uv run python knowledge.py --help
uv run python knowledge.py build
uv run python knowledge.py search "example terms"
```

The Typer application is the primary CLI. Most commands support `--help`; read commands that return
structured data support `--json`.

Common commands:

```sh
uv run python knowledge.py build [--check]
uv run python knowledge.py search "terms" [--subject SUBJECT] [--limit N]
uv run python knowledge.py facts [--subject SUBJECT] [--status STATUS]
uv run python knowledge.py show FACT_ID
uv run python knowledge.py history FACT_ID_OR_KEY
uv run python knowledge.py add-fact "statement" --subject SUBJECT --trust LEVEL
uv run python knowledge.py review-pending
uv run python knowledge.py approve FACT_ID_OR_KEY --reason "reviewed"
uv run python knowledge.py privacy check "statement" --subject SUBJECT
uv run python knowledge.py inbox
```

The inbox binds to loopback and displays private facts. Keep it local; do not expose it through a
proxy, tunnel, or port forward.

## Pipeline and extensions

`build.py` applies the core schema and runs the ordered steps in `build_rules.py`. The core pipeline
can build without optional sources. A checkout enables optional sources through its private
`local_paths.py`; each source may add schema fragments and ingest steps. The source inputs and any
source-specific configuration stay private. Code in `ingest/` is independent of optional adapters.

The built database and its normal-view copy are generated artifacts. Never edit either database by
hand. Change the source data or pipeline, then rebuild.

## Development

```sh
uv run pytest
uv run mypy
uv run tach check
uv run python tests/arch_check.py
```

The project uses a ports-and-adapters architecture. Domain rules are kept separate from file,
database, and process access; use cases depend on explicit ports; facades compose adapters; and the
Typer CLI and ingest pipeline drive those use cases. See [docs/architecture.md](docs/architecture.md)
for the module map and enforced boundaries.

Test fixtures must remain synthetic and generic. Before adding examples or generated snapshots,
check that they contain no private facts, names, paths, or client-specific data.
