"""The deterministic export format of the curated database (docs/db-source-of-truth.md, increment 2b), pure.

Domain service (tach layer `domain_services`): which tables are exported, how a row becomes one line of text, and the
manifest that proves a directory of such files is complete and unaltered. Nothing here reads a file or a database.

One file per table (`<table>.ndjson`): one JSON object per row, keys sorted, no insignificant whitespace, UTF-8, one
`\\n` after every row, rows in primary-key order. The same database always gives the same bytes; nothing in the files
depends on the time of the export. `manifest.json` lists the schema version and each file's row count and SHA-256.
"""

__arbor__ = {"ring": "domain_services"}

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

FORMAT = 1
MANIFEST_NAME = "manifest.json"

# Every table that holds data nothing else can recreate or that foreign keys point into, parents before children (the
# importer inserts in this order). Not exported: build_info and build_inputs (build provenance), the full-text index
# (rebuilt by the facts triggers) and the client tables (re-imported from their outside files).
EXPORT_TABLES = (
    "publishers", "authors", "vault_files", "sources", "source_authors", "source_identifiers",
    "source_identifier_conflicts", "source_relations", "subjects", "subject_aliases", "entities", "entity_aliases",
    "facts", "fact_sources", "fact_entities", "fact_revisions", "claims", "claim_facts",
)


class ExportError(Exception):
    """The export or import cannot proceed; the message says why."""


def file_name(table: str) -> str:
    """The name of a table's export file."""
    if table not in EXPORT_TABLES:
        raise ExportError(f"{table!r} is not an exported table")
    return f"{table}.ndjson"


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


@dataclass(frozen=True)
class Manifest:
    """What an export directory should contain: the schema version and, per table, (row count, SHA-256)."""
    user_version: int
    tables: Mapping[str, Sequence]


def build_manifest(user_version: int, texts: Mapping[str, str]) -> str:
    """The text of `manifest.json` for the given table texts (all of `EXPORT_TABLES`, nothing else)."""
    if set(texts) != set(EXPORT_TABLES):
        raise ExportError("the manifest needs exactly the exported tables")
    tables = {t: {"rows": texts[t].count("\n"), "sha256": digest(texts[t])} for t in EXPORT_TABLES}
    return json.dumps({"format": FORMAT, "schema_user_version": user_version, "tables": tables},
                      sort_keys=True, indent=2) + "\n"


def parse_manifest(text: str) -> Manifest:
    """The manifest in `text`; ExportError unless it is this format and lists exactly the exported tables."""
    try:
        data = json.loads(text)
        if data["format"] != FORMAT:
            raise ExportError(f"export format {data['format']!r}, this code reads format {FORMAT}")
        tables = {t: (int(v["rows"]), str(v["sha256"])) for t, v in data["tables"].items()}
        version = int(data["schema_user_version"])
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ExportError(f"manifest.json is not a valid manifest: {e!r}") from e
    if set(tables) != set(EXPORT_TABLES):
        raise ExportError("manifest.json does not list exactly the exported tables")
    return Manifest(version, tables)


def drift(manifest: Manifest, texts: Mapping[str, str]) -> List[str]:
    """One line per table whose text differs from the manifest (row count or hash); empty when they agree."""
    problems = []
    for table in EXPORT_TABLES:
        rows, sha = manifest.tables[table]
        text = texts.get(table)
        if text is None:
            problems.append(f"{table}: file is missing")
        elif digest(text) != sha or text.count("\n") != rows:
            problems.append(f"{table}: differs from the manifest (manifest {rows} rows, file {text.count(chr(10))})")
    return problems


def read_texts(read: Callable[[str], str]) -> Dict[str, str]:
    """The text of every table file that exists, read through `read(file name)` (FileNotFoundError: absent)."""
    texts: Dict[str, str] = {}
    for table in EXPORT_TABLES:
        try:
            texts[table] = read(file_name(table))
        except FileNotFoundError:
            pass
    return texts


def read_manifest(read: Callable[[str], str]) -> Manifest:
    """The manifest read through `read`; ExportError when it is missing or invalid."""
    try:
        return parse_manifest(read(MANIFEST_NAME))
    except FileNotFoundError as e:
        raise ExportError("manifest.json is missing: this is not a complete export") from e
