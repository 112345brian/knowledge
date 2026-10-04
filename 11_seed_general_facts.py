"""Load general_facts.json (in knowledge-private, not this repo): ad hoc
facts with no project/vault behind them (no bodybuilding.db, no
sources/*.md frontmatter, often no source at all) -- the stray thing you
noticed and want on record. Add entries via add_fact.py, which enforces the
shape this script expects; don't hand-edit the JSON.

New subjects created here default to domain='general' rather than the
health-and-fitness default other scripts rely on.

Run last -- has no dependency on anything but schema.sql, but nothing else
depends on it either.
"""
import sqlite3, json, os

from paths import PRIVATE_DATA_DIR as DATA_DIR
import privacy

# Documented fallback for entries with no `date_added` of their own (add_fact.py
# always writes one). It is the date the first batch was loaded, kept so rebuilds
# don't change those rows.
LEGACY_DATE_ADDED = "2026-09-26"
VALID_TRUST = {"verified", "high", "medium", "low", "unverified", "disputed"}
VALID_VISIBILITY = {"private", "normal"}  # keep in sync with the CHECK on facts.visibility


def get_or_create_subject(cur, name, domain, cache):
    if name in cache:
        return cache[name]
    row = cur.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
    if row:
        sid = row[0]
    else:
        cur.execute("INSERT INTO subjects (name, domain) VALUES (?, ?)", (name, domain))
        sid = cur.lastrowid
    cache[name] = sid
    return sid


def run(con):
    # Fail early on a corrupt rules file; an absent one means empty rules (#31).
    rules = privacy.load_rules(os.path.join(DATA_DIR, privacy.RULES_FILENAME))
    cur = con.cursor()
    citekey_to_id = {r[0]: r[1] for r in cur.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}
    subject_cache = {}

    items = json.load(open(os.path.join(DATA_DIR, "general_facts.json")))
    inserted = skipped = 0
    bad_citekeys = set()

    for item in items:
        subj = (item.get("subject") or "").strip()
        stmt = (item.get("statement") or "").strip()
        trust = (item.get("trust_level") or "").strip()
        if not subj or not stmt or trust not in VALID_TRUST:
            skipped += 1
            continue
        visibility = item.get("visibility")
        if visibility is None:
            visibility = "private"  # unmarked facts are private; never derived from is_personal
        if not isinstance(visibility, str) or visibility not in VALID_VISIBILITY:
            print(f"  WARNING -- skipping fact with invalid visibility {visibility!r}: {stmt[:60]!r}")
            skipped += 1
            continue

        subject_id = get_or_create_subject(cur, subj, item.get("domain") or "general", subject_cache)
        is_original = 1 if item.get("is_original_claim") else 0
        is_personal = 1 if item.get("is_personal", True) else 0

        date_added = item.get("date_added") or LEGACY_DATE_ADDED
        cur.execute(
            """INSERT INTO facts (subject_id, statement, is_original_claim, is_personal, trust_level, trust_rationale,
                                   provided_by, date_added, last_reviewed_at, notes, recheck_by, recheck_rationale, visibility,
                                   captured_via, session_id, captured_at, source_quote)
               VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject_id, stmt, is_original, is_personal, trust, item.get("trust_rationale"),
             date_added, date_added, item.get("notes"),
             item.get("recheck_by"), item.get("recheck_rationale"), visibility,
             item.get("captured_via"), item.get("session_id"), item.get("captured_at"), item.get("source_quote"))
        )
        fact_id = cur.lastrowid

        citekey = item.get("source_citekey")
        if citekey:
            source_id = citekey_to_id.get(citekey)
            if source_id is None:
                bad_citekeys.add(citekey)
            else:
                cur.execute(
                    "INSERT OR IGNORE INTO fact_sources (fact_id, source_id, locator, quote) VALUES (?, ?, ?, ?)",
                    (fact_id, source_id, item.get("source_locator"), item.get("source_quote"))
                )

        inserted += 1

    # Re-apply the current privacy rules to every fact (raise-only; also tags subjects).
    # This is the last build step, so subjects' parent_id (step 06) is set and tags inherit.
    applied = privacy.apply_rules_to_db(con, rules)
    con.commit()
    if applied["raised"]:
        print(f"  privacy rules raised {len(applied['raised'])} fact(s) to private")
    print(f"[11_seed_general_facts] inserted {inserted}, skipped {skipped} (bad shape)")
    if bad_citekeys:
        print(f"  WARNING -- citekeys referenced but not found in sources: {sorted(bad_citekeys)}")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
