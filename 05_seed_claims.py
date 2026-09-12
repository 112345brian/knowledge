"""Hand-authored broader claims, each citing the specific facts backing it.
Run last (needs facts to exist)."""
import sqlite3, os

CLAIMS = [
    dict(
        statement="Do not use anabolic steroids at this time.",
        notes=("Vault position as of 2026-08-20 review, per Current Recommendations.md \"On steroids\" section: "
               "the risk case is weaker than previously stated, but so is the benefit, and two time-sensitive open "
               "questions (unexplained FSH, falling bone Z-score) would be permanently or additionally confounded "
               "by starting now."),
        fact_match_prefixes=[
            "Whole-body BMD Z-score fell from -0.6",
            "The FSH result in the most recent hormone panel",
            "In the best-matched prospective study available (Verdegaal",
        ],
    ),
    dict(
        statement=("Target training volume for hypertrophy should sit around 10-20 sets/muscle/week, not higher -- "
                    "current logged volume is well below this floor for every muscle group, so the binding "
                    "constraint is consistency, not the target dose itself."),
        notes="Vault position per Current Recommendations.md \"What changed\" table: 63 of 394 weeks ever hit 3+ sessions.",
        fact_match_prefixes=["Approximately 10 to 20 sets per muscle per week"],
    ),
]


def run(con):
    cur = con.cursor()
    for c in CLAIMS:
        cur.execute("INSERT INTO claims (statement, notes) VALUES (?, ?)", (c["statement"], c["notes"]))
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
