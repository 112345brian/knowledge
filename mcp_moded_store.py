"""The MCP server's reads and writes, held to the session's mode (off / normal / private; the rules are in `modes`).

Infrastructure ring (tach layer `infrastructure`): the adapter that gives `mcp_port.FactLookup` its mode-filtered answer
(over `modes_store`, the only reader that applies the filter in SQL) and wraps the add-fact use case so an `off`
database refuses a write before anything is touched. `normal` hides private facts and subjects exactly as the CLI's
normal mode does; `private` sees everything; `off` raises `modes.ModeOff` on every read and write, which the MCP
use case reports as a tool error.

Writes are otherwise unchanged: an MCP fact is always stored private and pending, and a person reviews it.
"""

__arbor__ = {"ring": "infrastructure"}

import sqlite3

import fact_queries
import modes
import modes_store
from fact_reads_port import FactQueryError


class ModedFactLookup:
    """`mcp_port.FactLookup` over `modes_store`: each call opens a read-only connection and closes it."""

    def __init__(self, db_path, session):
        self._db_path = db_path
        self._session = session

    def _read(self, fn, *args, **kwargs):
        modes.readable(self._session, "reading the knowledge base")   # off: refuse before the file is even opened
        con = fact_queries.connect(self._db_path)
        try:
            return fn(self._session, con, *args, **kwargs)
        except sqlite3.OperationalError as e:
            raise FactQueryError(str(e)) from e
        finally:
            con.close()

    def search_facts(self, terms, **filters):
        """Matching facts under the mode: private facts and private subjects never appear in normal mode."""
        return self._read(modes_store.search_facts, terms, **filters)

    def list_subjects(self, include_pending=False):
        """The subjects the mode may see, with their visible-fact counts."""
        # The visible-fact count is `modes_store`'s (active facts; everything in private mode), so pending ones
        # are not counted separately here.
        return self._read(modes_store.list_subjects)


class ModedCapture:
    """`mcp_port.FactCapture` that refuses every write while the mode is `off`, else defers to `inner`."""

    def __init__(self, inner, session):
        self._inner = inner
        self._session = session

    def record_fact(self, fact, data_path=None, db_path=None, allow_dirty=False):
        """Record `fact` through the inner use case, or raise `modes.ModeOff` without touching anything."""
        modes.readable(self._session, "writing")
        return self._inner.record_fact(fact, data_path=data_path, db_path=db_path, allow_dirty=allow_dirty)
