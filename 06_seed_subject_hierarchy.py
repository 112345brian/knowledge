"""Arrange subjects into a shallow tree: aas-* subtopics under
'anabolic-steroids', training-* subtopics under a new 'training' umbrella.
Run after 04_ingest_facts.py (subjects are created on the fly during fact
ingestion, so they must exist first).
"""
import sqlite3, os

from seed_rules import AAS_CHILDREN, TRAINING_CHILDREN


def run(con):
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO subjects (name, domain) VALUES ('training', 'health-and-fitness')")

    cur.execute(
        "UPDATE subjects SET parent_id = (SELECT id FROM subjects WHERE name = 'anabolic-steroids') "
        f"WHERE name IN ({','.join('?' * len(AAS_CHILDREN))})", AAS_CHILDREN
    )
    cur.execute(
        "UPDATE subjects SET parent_id = (SELECT id FROM subjects WHERE name = 'training') "
        f"WHERE name IN ({','.join('?' * len(TRAINING_CHILDREN))})", TRAINING_CHILDREN
    )
    con.commit()
    n = cur.execute("SELECT COUNT(*) FROM subjects WHERE parent_id IS NOT NULL").fetchone()[0]
    print(f"[06_seed_subject_hierarchy] {n} subjects now have a parent")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
