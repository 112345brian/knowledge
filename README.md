# Knowledge

A personal knowledge base built as a local SQLite database from reviewed facts and source records,
with a CLI for ingesting, reviewing, searching, and auditing them.

- **Provenance:** every fact carries a trust level and its sources; claims can be audited against
  the facts they cite.
- **History:** changes are appended as revisions, never overwritten, so any fact has a full history.
- **Privacy:** facts are `normal` or `private`; privacy rules are checked before a fact is stored.
- **Reproducible:** the database is a build artifact. Edit the inputs or the code, then rebuild.

## Contents

- [Data boundary](#data-boundary)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Usage](#usage)
- [How the build works](#how-the-build-works)
- [Project layout](#project-layout)
- [Development](#development)
- [License](#license)

## Data boundary

This repository holds code, schemas, and synthetic test fixtures only. It must not contain real
facts, private configuration, local file paths, or client data. Those live in a separate private
data directory.

- The private directory supplies `local_paths.py`, which defines the database location, the input
  paths, and the enabled optional sources.
- Point to it with `KNOWLEDGE_PRIVATE_DIR` unless it sits next to this repository.
- Nothing from it may appear in source files, docs, examples, snapshots, or test goldens. Tests
  build synthetic data in temporary directories.

## Requirements

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)

## Quick start

```sh
uv sync
export KNOWLEDGE_PRIVATE_DIR=/path/to/private-data
uv run python knowledge.py build
uv run python knowledge.py search "example terms"
```

`build --check` builds into a throwaway file and reports counts without touching the live database.

## Usage

`knowledge.py` is the single entry point. Run `uv run python knowledge.py --help` for every command
and `<command> --help` for its options. Read commands accept `--json`.

| Task | Command |
| --- | --- |
| Rebuild the database | `build [--check]` |
| Full-text search | `search "terms" [--subject S] [--trust LEVEL] [--limit N]` |
| List or filter facts | `facts [--subject S] [--status STATUS]` |
| Show one fact | `show FACT_ID [--as-of DATE]` |
| Show a fact's revisions | `history FACT_ID_OR_KEY` |
| Add a fact | `add-fact "statement" --subject S --trust LEVEL` |
| Add a reviewed batch | `add-facts` |
| Review pending facts | `review-pending`, `approve REF...`, `reject REF... --reason TEXT` |
| Change a fact | `edit`, `supersede`, `retract`, `set-visibility` |
| Check privacy | `privacy check "statement" --subject S` |
| Audit | `audit-claims`, `audit-sources`, `audit-source-status`, `build-info` |
| Manage vocabularies | `subject ...`, `entity ...`, `source ...` |

Prefix each command with `uv run python knowledge.py`.

### Review inbox

`knowledge.py inbox` serves the pending-fact review page on `127.0.0.1`. It displays private facts,
so keep it local: do not put it behind a proxy, tunnel, or port forward.

## How the build works

`build.py` applies `schema.sql` and runs the ordered steps in `build_rules.py`. The core pipeline
builds without any optional source. A checkout enables optional sources (under `client/`) through
its private `local_paths.py`; each may add schema fragments and ingest steps. Code in `ingest/` never
imports from `client/`.

The built database and its normal-view copy are generated. Never edit either by hand.

Procedures for registering facts are in [docs/register-facts-protocol.md](docs/register-facts-protocol.md).

## Project layout

| Path | Contents |
| --- | --- |
| `knowledge.py`, `cli_*.py` | Typer CLI (driving adapter) |
| `build.py`, `build_rules.py`, `ingest/` | Build pipeline and core ingest steps |
| `client/` | Optional sources, enabled per checkout |
| `*_service.py` | Application services that orchestrate rules through ports |
| `*_rules.py`, `*_port.py` | Pure domain rules and port protocols |
| `*_store.py`, `paths.py`, `clock.py` | Adapters for the database, files, git, and time |
| `schema.sql` | Core database schema |
| `docs/` | [Architecture](docs/architecture.md), [competency questions](docs/competency-questions.md), [fact registration protocol](docs/register-facts-protocol.md) |
| `tests/` | Test suite and synthetic fixtures |

The code follows an onion architecture: dependencies point inward, domain rules do no I/O, and
services reach files, the database, and processes only through ports. [docs/architecture.md](docs/architecture.md)
has the module map and the enforced boundaries.

## Development

```sh
uv run pytest                        # full suite, parallel by default
uv run mypy
uv run tach check                    # module layering
uv run python tests/arch_check.py    # tach + import-linter + repository scan
```

Test fixtures must stay synthetic. Before adding examples or generated snapshots, check that they
contain no private facts, names, paths, or client-specific data.

Every action available in the UI is also a CLI command; see `cli_parity.py` and
`tests/test_cli_parity.py`.

## License

[MIT](LICENSE).
