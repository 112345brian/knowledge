"""Apply fact_revisions.jsonl (issue #30): validate the append-only revision log, insert
revisions >= 2 into `fact_revisions`, and write each touched fact's latest revision into the
`facts` row (statement, trust, status, visibility, superseded_by_fact_id, recheck, notes).

Revision 1 (the original entry) was already inserted by 04/11. A violation (gap or duplicate
revision number, timestamp going backwards, unknown source_key, unresolvable superseded_by,
malformed line) fails the build and names `file:line`.

Run after the fact-loading steps (04 and 11). A missing or empty log is fine.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import revisions
from paths import PRIVATE_DATA_DIR as DATA_DIR


def run(con):
    path = os.path.join(DATA_DIR, revisions.REVISIONS_FILENAME)
    applied = revisions.apply_revisions(con, path)
    total = con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0]
    print(f"[12_apply_fact_revisions] applied {applied} revisions ({total} rows in fact_revisions incl. implicit revision 1)")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
