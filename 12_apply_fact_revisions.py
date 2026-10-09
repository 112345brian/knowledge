"""Apply fact_revisions.jsonl (issue #30): validate the append-only revision log, insert
revisions >= 2 into `fact_revisions`, and write each touched fact's latest revision into the
`facts` row (statement, trust, status, visibility, superseded_by_fact_id, recheck, notes).

Revision 1 (the original entry) was already inserted by 04/11. A violation (gap or duplicate
revision number, timestamp going backwards, unknown source_key, unresolvable superseded_by,
malformed line) fails the build and names `file:line`.

Run after the fact-loading steps (04 and 11). A missing or empty log is fine.

The privacy floor (#31) holds after revisions are applied: writing a revision back into `facts`
could otherwise lower a visibility that 04/11 had raised (or miss a name a reworded statement now
contains), so the current rules are re-applied here, raise-only. History is never more visible than
the fact itself: every revision snapshot of a private fact is raised to private too.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import privacy
import privacy_store
import revisions
import revisions_store
from paths import PRIVATE_DATA_DIR as DATA_DIR


def run(con):
    path = os.path.join(DATA_DIR, revisions.REVISIONS_FILENAME)
    applied = revisions_store.apply_revisions(con, path)
    rules = privacy_store.load_rules(os.path.join(DATA_DIR, privacy.RULES_FILENAME))
    floor = privacy_store.apply_rules_to_db(con, rules)
    con.execute("""UPDATE fact_revisions SET visibility = 'private'
                   WHERE visibility != 'private'
                     AND fact_id IN (SELECT id FROM facts WHERE visibility = 'private')""")
    con.commit()
    if floor["raised"]:
        print(f"  privacy rules raised {len(floor['raised'])} fact(s) to private after revisions")
    total = con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0]
    print(f"[12_apply_fact_revisions] applied {applied} revisions ({total} rows in fact_revisions incl. implicit revision 1)")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
