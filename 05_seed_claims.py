"""Hand-authored broader claims, each citing the specific facts backing it.
Run last (needs facts to exist).

A claim may also carry a `warrant` (why the cited grounds support it) and a `qualifier` (how far it holds), #44.
`fact_match_prefixes` entries are either a statement prefix (a plain string: the fact is `grounds`) or a dict
{"prefix": ..., "role": "grounds" | "backing" | "rebuttal", "note": "why this fact is linked"}. Blank warrant,
qualifier and note are stored as NULL; an unknown role fails the build."""
import sqlite3, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims_audit

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


def _blank_to_none(value, what):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, not {type(value).__name__}")
    return value.strip() or None


def _premise(entry, claim_statement):
    """(prefix, role, note) from a plain prefix string or a {"prefix", "role", "note"} dict."""
    if isinstance(entry, str):
        return entry, "grounds", None
    if not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str) or not entry["prefix"]:
        raise ValueError(f"claim {claim_statement[:50]!r}: each fact_match_prefixes entry must be a string or a dict with a 'prefix'")
    role = entry.get("role", "grounds")
    if role not in claims_audit.ROLES:
        raise ValueError(f"claim {claim_statement[:50]!r}: role {role!r} must be one of {list(claims_audit.ROLES)}")
    return entry["prefix"], role, _blank_to_none(entry.get("note"), "note")


def run(con):
    cur = con.cursor()
    for c in CLAIMS:
        # inference_type is optional and forward-only: the two claims above are deliberately unclassified.
        # warrant / qualifier (#44) are optional too: the two claims above are not backfilled beyond the defaults.
        cur.execute("INSERT INTO claims (statement, notes, inference_type, warrant, qualifier) VALUES (?, ?, ?, ?, ?)",
                    (c["statement"], c["notes"], c.get("inference_type"),
                     _blank_to_none(c.get("warrant"), "warrant"), _blank_to_none(c.get("qualifier"), "qualifier")))
        claim_id = cur.lastrowid
        for entry in c["fact_match_prefixes"]:
            prefix, role, note = _premise(entry, c["statement"])
            cur.execute(
                "INSERT INTO claim_facts (claim_id, fact_id, role, note) SELECT ?, id, ?, ? FROM facts WHERE statement LIKE ?",
                (claim_id, role, note, prefix + "%")
            )
    con.commit()
    print(f"[05_seed_claims] inserted {len(CLAIMS)} claims")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
