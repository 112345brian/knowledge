"""Load pilot_facts.json (hand-authored) plus facts_batch1-4.json (extracted by
background agents reading the vault's top-level synthesis notes and
harm-reduction files -- see project_knowledge_db memory for how those were
produced) into `facts`, `fact_sources`, `fact_subjects`. (The `measurements` client source then links facts to
measurements: client/link_fact_measurements.py.)

Classifies is_personal with a multi-signal heuristic (documented inline) --
this is a best-effort first pass, not hand-verified per fact.

Run after seed_sources and literature_sources (needs sources + subjects to exist).
"""
import sqlite3, json, os, sys

from ingest import fact_ingest_rules
import fixity_store
from ingest.shared import get_or_create_vault_file, load_hints
import revisions
import revisions_store
from paths import BODYBUILDING_VAULT as VAULT, PRIVATE_DATA_DIR as DATA_DIR
import privacy
import privacy_store

def load_items():
    """All entries, each with `_is_pilot`, `_where` (file, index) and `_source_key` (its own, or the deterministic
    legacy-... key from revisions.derive_keys until backfill_source_keys.py writes one)."""
    items = []
    for name, is_pilot in [("pilot_facts.json", True)] + [(f"facts_batch{i}.json", False) for i in range(1, 5)]:
        batch = json.load(open(os.path.join(DATA_DIR, name)))
        for index, (item, key) in enumerate(zip(batch, revisions.derive_keys(batch, name))):
            item["_is_pilot"] = is_pilot
            item["_source_key"] = key
            item["_where"] = (name, index)
        items.extend(batch)
    return items


def get_or_create_subject(cur, name, cache):
    if name in cache:
        return cache[name]
    row = cur.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
    if row:
        sid = row[0]
    else:
        cur.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
        sid = cur.lastrowid
    cache[name] = sid
    return sid


def run(con):
    # Fail early on a corrupt rules file; an absent one means empty rules (#31).
    rules = privacy_store.load_rules(os.path.join(DATA_DIR, privacy.RULES_FILENAME))
    hints = load_hints(DATA_DIR)   # the author's vault hints (fact_hints.json), or none
    cur = con.cursor()
    citekey_to_id = {r[0]: r[1] for r in cur.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}
    subject_cache = {}

    items = load_items()
    inserted = skipped = 0
    bad_citekeys = set()
    derived = 0

    for item in items:
        try:
            subj, stmt, trust, visibility, status = fact_ingest_rules.screen_item(item)
        except fact_ingest_rules.Skip as skip:
            if skip.warning:
                print(skip.warning)
            skipped += 1
            continue
        key = fact_ingest_rules.check_source_key(item["_source_key"], stmt)
        if cur.execute("SELECT 1 FROM facts WHERE source_key = ?", (key,)).fetchone():
            raise ValueError(f"duplicate source_key {key!r} (fact {stmt[:60]!r})")
        derived += 0 if item.get("source_key") else 1

        subject_id = get_or_create_subject(cur, subj, subject_cache)

        origin_path = item.get("origin_path") or fact_ingest_rules.resolve_origin_path(item.get("notes"), VAULT, hints)
        origin_file_id = get_or_create_vault_file(cur, origin_path)
        measured_metric = item.get("measurement_metric_link")
        is_original = 1 if item.get("is_original_claim") else 0
        is_personal = fact_ingest_rules.classify_is_personal(stmt, item.get("notes"), is_original, measured_metric, hints)

        baseline = item.get("extracted_from_sha256")
        if baseline is not None and not fixity_store.valid_sha256(baseline):
            raise ValueError(f"invalid extracted_from_sha256 {baseline!r} on fact {stmt[:60]!r} (want 64 lowercase hex)")
        date_added = fact_ingest_rules.require_date_added(item, *item["_where"])
        # #7: the entry's own freshness, or for a legacy entry (original files, no provenance)
        # 'recheck' if it has a recheck_by, else 'unreviewed' plus the "predates this field" note.
        # Anything else fails the build.
        eff = revisions.effective_entry(item, item["_where"][0])
        cur.execute(
            """INSERT INTO facts (subject_id, statement, is_original_claim, is_personal, trust_level, trust_rationale,
                                   provided_by, date_added, last_reviewed_at, notes, recheck_by, recheck_rationale, origin_file_id, visibility,
                                   captured_via, session_id, captured_at, source_quote, status, source_key, freshness,
                                   extracted_from_sha256, kind, valid_from, valid_to, applies_to)
               VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (subject_id, stmt, is_original, is_personal, trust, item.get("trust_rationale"),
             date_added, date_added, eff.get("notes"), item.get("recheck_by"), item.get("recheck_rationale"), origin_file_id, visibility,
             item.get("captured_via"), item.get("session_id"), item.get("captured_at"), item.get("source_quote"),
             status, key, eff["freshness"], baseline, revisions.entry_kind(item), *revisions.entry_validity(item), revisions.entry_applies_to(item))
        )
        fact_id = cur.lastrowid
        revisions_store.insert_revision_row(cur, fact_id, revisions.implicit_revision(key, item, date_added, item["_where"][0]))

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
    # Subjects' parent_id is set by step 06, so 11 (last) is the pass that sees the whole tree.
    applied = privacy_store.apply_rules_to_db(con, rules)
    con.commit()
    if applied["raised"]:
        print(f"  privacy rules raised {len(applied['raised'])} fact(s) to private")
    print(f"[facts] inserted {inserted}, skipped {skipped} (bad shape)")
    if bad_citekeys:
        print(f"  WARNING -- citekeys referenced but not found in sources: {sorted(bad_citekeys)}")
    if derived:
        print(f"  note: {derived} entries have no source_key yet; used deterministic legacy-* keys. "
              f"Run backfill_source_keys.py to write them into the files (same values).")
    print("  facts by freshness:", dict(cur.execute("SELECT freshness, COUNT(*) FROM facts GROUP BY freshness").fetchall()))
    print("  facts by trust_level:", dict(cur.execute("SELECT trust_level, COUNT(*) FROM facts GROUP BY trust_level").fetchall()))
    print("  facts by is_personal:", dict(cur.execute("SELECT is_personal, COUNT(*) FROM facts GROUP BY is_personal").fetchall()))


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
