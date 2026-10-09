"""Hand-authored broader claims, each citing the specific facts backing it.
Run last (needs facts to exist)."""
import sqlite3, os

from seed_rules import CLAIMS


def run(con):
    cur = con.cursor()
    for c in CLAIMS:
        # inference_type is optional and forward-only: the two claims above are deliberately unclassified.
        cur.execute("INSERT INTO claims (statement, notes, inference_type) VALUES (?, ?, ?)",
                    (c["statement"], c["notes"], c.get("inference_type")))
        claim_id = cur.lastrowid
        for prefix in c["fact_match_prefixes"]:
            cur.execute(
                "INSERT INTO claim_facts (claim_id, fact_id) SELECT ?, id FROM facts WHERE statement LIKE ?",
                (claim_id, prefix + "%")
            )
    con.commit()
    print(f"[05_seed_claims] inserted {len(CLAIMS)} claims")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
