"""The deterministic export format of the database (docs/db-source-of-truth.md, "One backup folder"), pure.

Domain service (tach layer `domain_services`): which tables go in which part of the export, how a row becomes one line
of text, and the manifests that prove a part is complete and unaltered. Nothing here reads a file or a database.

The export folder has up to three parts, each with its own manifest:

- `core/`: the curated tables (`CORE_TABLES`) and, when the database has them, the logs (`LOG_TABLES`);
- `bulk/`: every table a client source owns (music, measurements, ...). The base does not know these tables: the
  client registry hands them in;
- `notes/`: the citation-note markdown files, copied only on request.

Parts are only a folder layout. WHAT is exported is a selection: named tables, named groups of tables, minus skipped
tables (`select`), so one table can be exported, verified or restored without the rest. A manifest lists every table
exported into its part so far: a partial export merges into what the folder already holds.

A table is one file `<part>/<table>.ndjson`: one JSON object per row, keys sorted, no insignificant whitespace, UTF-8,
one `\\n` after every row, rows in primary-key order. The same database always gives the same bytes; nothing depends on
the time of the export. `<part>/manifest.json` lists the schema version and each file's row count and SHA-256.
"""

__arbor__ = {"ring": "domain_services"}

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

FORMAT = 1
CORE = "core"
BULK = "bulk"
NOTES = "notes"
TABLE_PARTS = (CORE, BULK)
MANIFEST = "manifest.json"

# Every table that holds data nothing else can recreate or that foreign keys point into, parents before children.
# Not exported: build_info and build_inputs (build provenance) and the full-text index (rebuilt by the facts triggers).
CORE_TABLES = (
    "publishers", "authors", "vault_files", "sources", "source_authors", "source_identifiers",
    "source_identifier_conflicts", "source_relations", "subjects", "subject_aliases", "entities", "entity_aliases",
    "facts", "fact_sources", "fact_entities", "fact_revisions", "claims", "claim_facts",
)
NOT_EXPORTED = ("build_info", "build_inputs", "audit_context")
# The live database's logs (live.sql): exported when the database has them, never required.
LOG_TABLES = ("audit_log", "access_log", "access_summary", "operation_log")
LIVE_SCHEMA = "live.sql"
# Named groups of tables for `--group`. A client source's name is also a group (its tables), added by the client registry.
GROUPS = {
    "facts": ("facts", "fact_revisions", "fact_sources", "fact_entities"),
    "sources": ("publishers", "authors", "vault_files", "sources", "source_authors", "source_identifiers",
                "source_identifier_conflicts", "source_relations"),
    "subjects": ("subjects", "subject_aliases"),
    "entities": ("entities", "entity_aliases"),
    "claims": ("claims", "claim_facts"),
    "logs": LOG_TABLES,
}

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_NOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.,'()\[\]&+-]*\.md$")


class ExportError(Exception):
    """The export or import cannot proceed; the message says why."""


class MissingPart(ExportError):
    """A part's manifest does not exist (the part was never exported); an invalid manifest is a plain ExportError."""


def check_table(table: str) -> str:
    """`table` if it is a plain lower-case SQL name (it becomes a file name and part of a statement), else ExportError."""
    if not isinstance(table, str) or not _IDENT.match(table):
        raise ExportError(f"{table!r} is not a valid table name")
    return table


def check_note(name: str) -> str:
    """`name` if it is a plain `*.md` file name with no directory part, else ExportError."""
    if not isinstance(name, str) or not _NOTE.match(name) or ".." in name:
        raise ExportError(f"{name!r} is not a valid note file name")
    return name


def manifest_name(part: str) -> str:
    """Path of a part's manifest inside the export folder."""
    return f"{part}/{MANIFEST}"


def file_name(part: str, table: str) -> str:
    """Path of a table's file inside the export folder."""
    if part not in TABLE_PARTS:
        raise ExportError(f"{part!r} is not a table part")
    return f"{part}/{check_table(table)}.ndjson"


def note_path(name: str) -> str:
    """Path of a note inside the export folder."""
    return f"{NOTES}/{check_note(name)}"


def encode_row(row: Mapping[str, Any]) -> str:
    """One row as one line (no newline). A value that is not null, an integer, a finite float or text is refused: a
    blob or NaN would not survive the round trip."""
    for key, value in row.items():
        if value is not None and not isinstance(value, (int, float, str)) or isinstance(value, bool):
            raise ExportError(f"column {key!r} holds a {type(value).__name__}, which the export cannot carry")
        if isinstance(value, float) and not math.isfinite(value):
            raise ExportError(f"column {key!r} holds {value!r}, which the export cannot carry")
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def encode_table(rows: Iterable[Mapping[str, Any]]) -> str:
    """The text of a table's file: every row's line followed by a newline (empty text for no rows)."""
    return "".join(encode_row(r) + "\n" for r in rows)


def decode_table(text: str) -> List[Dict[str, Any]]:
    """The rows of a table file. A line that is not a JSON object, or a file that does not end in a newline, is an error."""
    if text and not text.endswith("\n"):
        raise ExportError("the file does not end with a newline (truncated?)")
    rows = []
    for number, line in enumerate(text.split("\n")[:-1] if text else [], 1):
        try:
            row = json.loads(line)
        except ValueError as e:
            raise ExportError(f"line {number} is not JSON: {e}") from e
        if not isinstance(row, dict):
            raise ExportError(f"line {number} is not a JSON object")
        rows.append(row)
    return rows


def digest(text: str) -> str:
    """SHA-256 (hex) of the text's UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def line_count(text: str) -> int:
    """Number of rows (lines) in a table text."""
    return text.count("\n")


@dataclass(frozen=True)
class Manifest:
    """What a part of an export should contain: the schema version and, per item, (row count, SHA-256). For a notes
    part the items are file names and the row count is 1."""
    user_version: int
    tables: Mapping[str, Sequence]


def part_of(table: str) -> str:
    """The part a table is written into: the core tables and the logs go in `core`, every other table in `bulk`."""
    return CORE if table in CORE_TABLES or table in LOG_TABLES else BULK


def build_manifest(user_version: int, texts: Mapping[str, str], previous: "Manifest | None" = None) -> str:
    """The text of a part's manifest for the given item texts. Items of `previous` that are not in `texts` are kept
    (a partial export adds to the folder), unless `previous` is from another schema version, which makes them stale."""
    items = {}
    if previous is not None and previous.user_version == user_version:
        items = {n: {"rows": r, "sha256": h} for n, (r, h) in previous.tables.items() if n not in texts}
    items.update({name: {"rows": line_count(text) if not name.endswith(".md") else 1, "sha256": digest(text)}
                  for name, text in texts.items()})
    items = dict(sorted(items.items()))
    return json.dumps({"format": FORMAT, "schema_user_version": user_version, "tables": items},
                      sort_keys=True, indent=2) + "\n"


def parse_manifest(text: str, part: str) -> Manifest:
    """The manifest in `text`; ExportError unless it is this format and lists valid items. The core part must list
    exactly `CORE_TABLES`; the bulk part any valid table names; the notes part valid note names."""
    try:
        data = json.loads(text)
        if data["format"] != FORMAT:
            raise ExportError(f"export format {data['format']!r}, this code reads format {FORMAT}")
        tables = {t: (int(v["rows"]), str(v["sha256"])) for t, v in data["tables"].items()}
        version = int(data["schema_user_version"])
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ExportError(f"{manifest_name(part)} is not a valid manifest: {e!r}") from e
    for name in tables:
        (check_note if part == NOTES else check_table)(name)
    if part == CORE and not set(tables) <= set(CORE_TABLES) | set(LOG_TABLES):
        raise ExportError(f"{manifest_name(part)} lists a table that is not a core table")
    return Manifest(version, tables)


def drift(manifest: Manifest, texts: Mapping[str, str]) -> List[str]:
    """One line per item whose text differs from the manifest (row count or hash) or is missing; empty when they agree."""
    problems = []
    for name, (rows, sha) in manifest.tables.items():
        text = texts.get(name)
        if text is None:
            problems.append(f"{name}: file is missing")
        elif digest(text) != sha or (not name.endswith(".md") and line_count(text) != rows):
            problems.append(f"{name}: differs from the manifest")
    return problems


def read_manifest(read: Callable[[str], str], part: str) -> Manifest:
    """The part's manifest read through `read(path)`; ExportError when it is missing or invalid."""
    try:
        return parse_manifest(read(manifest_name(part)), part)
    except FileNotFoundError as e:
        raise MissingPart(f"{manifest_name(part)} is missing: this is not a complete export") from e


def read_texts(read: Callable[[str], str], part: str, manifest: Manifest) -> Dict[str, str]:
    """The text of every item the manifest lists that exists, read through `read(path)`."""
    texts: Dict[str, str] = {}
    for name in manifest.tables:
        path = note_path(name) if part == NOTES else file_name(part, name)
        try:
            texts[name] = read(path)
        except FileNotFoundError:
            pass
    return texts


def select(tables: Sequence[str], groups: Sequence[str], skip: Sequence[str], client_groups: Mapping[str, Sequence[str]],
           existing: Iterable[str], client_tables: Iterable[str]) -> List[str]:
    """The tables to export, in a fixed order (core tables, then logs, then client tables sorted): the named `tables`
    plus the tables of the named `groups`, or everything the database has when neither is given, minus `skip`.
    Naming a table the database lacks is an error; a group member the database lacks (the logs of a rebuilt
    database, a disabled client source) is just left out. Unknown names are errors that list the known ones."""
    existing = set(existing)
    client = sorted(set(client_tables))
    known_groups = dict(GROUPS)
    for name, members in client_groups.items():
        if name in known_groups:
            raise ExportError(f"client group {name!r} has the name of a base group")
        known_groups[name] = tuple(members)
    order = [*CORE_TABLES, *LOG_TABLES, *client]
    for name in (*tables, *skip):
        if name not in order:
            raise ExportError(f"{name!r} is not an exportable table; known: {', '.join(order)}")
    for name in groups:
        if name not in known_groups:
            raise ExportError(f"{name!r} is not a group; known: {', '.join(sorted(known_groups))}")
    absent = [t for t in tables if t not in existing]
    if absent:
        raise ExportError("the database lacks table(s): " + ", ".join(absent))
    if tables or groups:
        chosen = set(tables) | {t for g in groups for t in known_groups[g] if t in existing}
    else:
        missing = [t for t in CORE_TABLES if t not in existing]
        if missing:
            raise ExportError("the database lacks core table(s): " + ", ".join(missing))
        chosen = {t for t in order if t in existing}
    chosen -= set(skip)
    return [t for t in order if t in chosen]
