"""SQLite adapters of the fact ingest: `build_repositories_port.FactRepository` and `UnitOfWork`, and the revision
write of `fact_ingest_port.FactRevisionWriter`, over the build's connection.

They store what they are told and decide nothing: which entries load, the row's values, the duplicate check and the
order of the writes are `fact_ingest_service`'s. Each `add` allocates its row when called, in call order (the golden
dump compares the ids). The one rule the foundation's `NewFact` fixes is that an ingested fact was last reviewed the
day it was added.
"""
import sqlite3
from typing import Any, Mapping, Optional

import build_records as r
import revisions_store
from build_repositories_port import FactTally

_FACT_COLUMNS = ("subject_id", "statement", "is_original_claim", "is_personal", "trust_level", "trust_rationale",
                 "provided_by", "date_added", "last_reviewed_at", "notes", "recheck_by", "recheck_rationale",
                 "origin_file_id", "visibility", "captured_via", "session_id", "captured_at", "source_quote", "status",
                 "source_key", "freshness", "extracted_from_sha256", "kind", "valid_from", "valid_to", "applies_to")
_INSERT_FACT = (f"INSERT INTO facts ({', '.join(_FACT_COLUMNS)}) "
                f"VALUES ({', '.join(':' + c for c in _FACT_COLUMNS)})")
_TALLY_COLUMNS = ("freshness", "trust_level", "is_personal")   # the facts columns `tally` may group by (a whitelist, not SQL)


class SqliteFactRepository:
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def source_key_exists(self, source_key: str) -> bool:
        return self._con.execute("SELECT 1 FROM facts WHERE source_key = ?", (source_key,)).fetchone() is not None

    def find_by_source_key(self, source_key: str) -> Optional[r.FactId]:
        row = self._con.execute("SELECT id FROM facts WHERE source_key = ?", (source_key,)).fetchone()
        return r.FactId(row[0]) if row else None

    def add(self, fact: r.NewFact) -> r.FactId:
        row = {c: getattr(fact, c, None) for c in _FACT_COLUMNS}
        row["last_reviewed_at"] = fact.date_added
        return r.FactId(self._con.execute(_INSERT_FACT, row).lastrowid)

    def link_source(self, link: r.FactSourceLink) -> None:
        self._con.execute("INSERT OR IGNORE INTO fact_sources (fact_id, source_id, locator, quote) VALUES (?, ?, ?, ?)",
                          (link.fact_id, link.source_id, link.locator, link.quote))

    def link_measurements(self, fact_id: r.FactId, metric_key: str) -> int:
        cur = self._con.execute(
            """INSERT INTO fact_measurements (fact_id, measurement_id)
               SELECT ?, m.id FROM measurements m JOIN metrics mt ON mt.id = m.metric_id WHERE mt.key = ?""",
            (fact_id, metric_key))
        return cur.rowcount

    def tally(self, by: FactTally) -> Mapping[Any, int]:
        if by not in _TALLY_COLUMNS:
            raise ValueError(f"cannot count facts by {by!r}; one of {list(_TALLY_COLUMNS)}")
        return dict(self._con.execute(f"SELECT {by}, COUNT(*) FROM facts GROUP BY {by} ORDER BY {by}").fetchall())


class SqliteFactRevisions:
    """`fact_ingest_port.FactRevisionWriter`: revision 1 of an ingested fact."""
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def insert(self, fact_id: r.FactId, revision: Mapping[str, object]) -> None:
        revisions_store.insert_revision_row(self._con, fact_id, revision)


class SqliteUnitOfWork:
    def __init__(self, con: sqlite3.Connection):
        self._con = con

    def commit(self) -> None:
        self._con.commit()
