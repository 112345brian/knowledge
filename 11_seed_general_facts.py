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
from _shared import get_or_create_vault_file
import fact_ingest_rules
import fixity_store
import privacy
import privacy_store
import revisions
import revisions_store

def get_or_create_subject(cur, name, domain, cache):
    if name in cache:
        return cache[name]
    row = cur.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
    if row is None:  # #43: an entry filed under an alias belongs to the canonical subject
        row = cur.execute("SELECT subject_id FROM subject_aliases WHERE alias = ?", (name,)).fetchone()
    if row:
        sid = row[0]
    else:
        cur.execute("INSERT INTO subjects (name, domain) VALUES (?, ?)", (name, domain))
        sid = cur.lastrowid
    cache[name] = sid
    return sid


def run(con):
    # Fail early on a corrupt rules file; an absent one means empty rules (#31).
    rules = privacy_store.load_rules(os.path.join(DATA_DIR, privacy.RULES_FILENAME))
    cur = con.cursor()
    citekey_to_id = {r[0]: r[1] for r in cur.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}
    subject_cache = {}

    items = json.load(open(os.path.join(DATA_DIR, "general_facts.json")))
    inserted = skipped = 0
    bad_citekeys = set()
    derived = 0

    for index, (item, key) in enumerate(zip(items, revisions.derive_keys(items, "general_facts.json"))):
        try:
            subj, stmt, trust, visibility, status = fact_ingest_rules.screen_item(item)
        except fact_ingest_rules.Skip as skip:
            if skip.warning:
                print(skip.warning)
            skipped += 1
            continue
        fact_ingest_rules.check_source_key(key, stmt)
        if cur.execute("SELECT 1 FROM facts WHERE source_key = ?", (key,)).fetchone():
            raise ValueError(f"duplicate source_key {key!r} (fact {stmt[:60]!r})")
        derived += 0 if item.get("source_key") else 1

        subject_id = get_or_create_subject(cur, subj, item.get("domain") or "general", subject_cache)
        is_original = 1 if item.get("is_original_claim") else 0
        is_personal = 1 if item.get("is_personal", True) else 0

        baseline = item.get("extracted_from_sha256")
        if baseline is not None and not fixity_store.valid_sha256(baseline):
            raise ValueError(f"invalid extracted_from_sha256 {baseline!r} on fact {stmt[:60]!r} (want 64 lowercase hex)")
        origin_file_id = get_or_create_vault_file(cur, item.get("origin_path"))  # #38: optional origin file
        date_added = fact_ingest_rules.require_date_added(item, "general_facts.json", index)
        # #7: general_facts.json is never legacy; an entry without a valid freshness fails the build.
        eff = revisions.effective_entry(item, "general_facts.json")
        cur.execute(
            """INSERT INTO facts (subject_id, statement, is_original_claim, is_personal, trust_level, trust_rationale,
                                   provided_by, date_added, last_reviewed_at, notes, recheck_by, recheck_rationale, visibility,
                                   captured_via, session_id, captured_at, source_quote, status, source_key, freshness,
                                   origin_file_id, extracted_from_sha256, kind, valid_from, valid_to, applies_to)
               VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject_id, stmt, is_original, is_personal, trust, item.get("trust_rationale"),
             date_added, date_added, item.get("notes"),
             item.get("recheck_by"), item.get("recheck_rationale"), visibility,
             item.get("captured_via"), item.get("session_id"), item.get("captured_at"), item.get("source_quote"),
             status, key, eff["freshness"], origin_file_id, baseline, revisions.entry_kind(item), *revisions.entry_validity(item), revisions.entry_applies_to(item))
        )
        fact_id = cur.lastrowid
        revisions_store.insert_revision_row(cur, fact_id, revisions.implicit_revision(key, item, date_added, "general_facts.json"))

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
    applied = privacy_store.apply_rules_to_db(con, rules)
    con.commit()
    if applied["raised"]:
        print(f"  privacy rules raised {len(applied['raised'])} fact(s) to private")
    print(f"[11_seed_general_facts] inserted {inserted}, skipped {skipped} (bad shape)")
    if bad_citekeys:
        print(f"  WARNING -- citekeys referenced but not found in sources: {sorted(bad_citekeys)}")
    if derived:
        print(f"  note: {derived} entries have no source_key yet; used deterministic legacy-* keys. "
              f"Run backfill_source_keys.py to write them into the files (same values).")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
