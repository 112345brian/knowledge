"""Load pilot_facts.json (hand-authored) plus facts_batch1-4.json (extracted by
background agents reading the vault's top-level synthesis notes and
harm-reduction files -- see project_knowledge_db memory for how those were
produced) into `facts`, `fact_sources`, `fact_subjects`, `fact_measurements`.

Classifies is_personal with a multi-signal heuristic (documented inline) --
this is a best-effort first pass, not hand-verified per fact.

Run after 01/02/03 (needs sources + subjects + measurements to exist).
"""
import sqlite3, json, os, re

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
VAULT = "<BODYBUILDING_VAULT>"
TODAY = "2026-09-11"
VALID_TRUST = {"verified", "high", "medium", "low", "unverified", "disputed"}

PRONOUN_RE = re.compile(r'\b(he|his|him|the vault owner|vault owner)\b', re.IGNORECASE)
FINGERPRINT_RE = re.compile(
    r'(26-year-old|26 years old|FFMI 15\.75|156\.4|163\.6|2025-11-15|2026-06-17|2025-01-24|'
    r'ankylosing spondylitis|Humira|BodySpec|adherence|13 lb weight loss)',
    re.IGNORECASE
)
TOP_LEVEL_PERSONAL_FILES = [
    "Current Recommendations", "Current State", "Goal Progress", "DEXA Decision Rules",
    "Body Measurement Tracker", "Restarting After a Gap", "Starting Sequence",
    "Strength Progression Baselines", "Where Sessions Break Down", "Where the Surplus Actually Comes From",
    "Rebalancing the Split", "Making the Calls", "Six-Month Test Protocol", "Program Design Constraints",
    "The Actual Decision", "Your First Cycle", "Cycle Preconditions", "The Case For",
    "Fitting It Into 45 Minutes", "Personal Trainer App Spec", "Fixing Ankle Dorsiflexion",
    "Loaded vs Static Ankle", "The Attractiveness Target", "The Exercise Screen", "The Program",
    "What the Physique Can and Cannot Buy", "Why Hasn't Mass Followed Strength",
    "Two-Year Body Composition Plan", "When To Train", "Volume Is the Variable", "What Muscle Actually Buys",
]

# Known-good vault-relative filename fixups for extraction-agent notes text that
# omitted the harm-reduction/ subfolder or abbreviated a filename. Applied to
# facts_batch*.json in the data/ directory before this script ever runs --
# this dict exists only so future extraction batches can reuse the same fixups
# without re-deriving them.
HARM_REDUCTION_FILES = {
    "AAS Cardiovascular Risk.md", "AAS Decision Framework.md", "AAS Emergency Red Flags.md",
    "AAS Endocrine Management.md", "AAS Liver and Kidney.md", "AAS Mental Health and Dependence.md",
    "AAS Myths Checked Against Evidence.md", "AAS Supply Testing and Legal Exposure.md",
    "AAS and Ankylosing Spondylitis.md", "AAS and the Law.md", "Ancillary Compounds Reference.md",
    "Bloodwork and Health Markers.md", "Cumulative Cycle Risk.md",
}


def resolve_origin_path(notes_text):
    """Extract the vault .md file a batch-extracted fact's `notes` references."""
    if not notes_text:
        return None
    m = re.search(r"(<BODYBUILDING_VAULT>/[^,]+?\.md)", notes_text)
    if m:
        return m.group(1)
    if notes_text.startswith("harm-reduction/"):
        m = re.match(r"^(harm-reduction/[^,]+?\.md)", notes_text)
        if m:
            return f"{VAULT}/{m.group(1)}"
    m = re.match(r'^([A-Za-z0-9][^,]*?\.md)', notes_text)
    if m:
        fname = m.group(1)
        if fname in HARM_REDUCTION_FILES:
            return f"{VAULT}/harm-reduction/{fname}"
        return f"{VAULT}/{fname}"
    # filenames containing a comma (e.g. "Volume Is the Variable, Not Frequency.md")
    # defeat the comma-terminated regex above -- fall back to a known list.
    for known in ["Volume Is the Variable, Not Frequency.md"]:
        if notes_text.startswith(known):
            return f"{VAULT}/{known}"
    return None


def classify_is_personal(statement, notes, is_original_claim, measured_link):
    text = f"{statement or ''} {notes or ''}"
    if measured_link:
        return 1
    if PRONOUN_RE.search(text):
        return 1
    if FINGERPRINT_RE.search(text):
        return 1
    if is_original_claim and any(f in (notes or "") for f in TOP_LEVEL_PERSONAL_FILES):
        return 1
    return 0


def load_items():
    items = json.load(open(os.path.join(DATA_DIR, "pilot_facts.json")))
    for item in items:
        item["_is_pilot"] = True
    for i in range(1, 5):
        batch = json.load(open(os.path.join(DATA_DIR, f"facts_batch{i}.json")))
        for item in batch:
            item["_is_pilot"] = False
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
    cur = con.cursor()
    citekey_to_id = {r[0]: r[1] for r in cur.execute("SELECT citekey, id FROM sources WHERE citekey IS NOT NULL")}
    subject_cache = {}

    items = load_items()
    inserted = skipped = 0
    bad_citekeys = set()

    for item in items:
        subj = (item.get("subject") or "").strip()
        stmt = (item.get("statement") or "").strip()
        trust = (item.get("trust_level") or "").strip()
        if not subj or not stmt or trust not in VALID_TRUST:
            skipped += 1
            continue

        subject_id = get_or_create_subject(cur, subj, subject_cache)

        origin_path = item.get("origin_path") or resolve_origin_path(item.get("notes"))
        measured_metric = item.get("measurement_metric_link")
        is_original = 1 if item.get("is_original_claim") else 0
        is_personal = classify_is_personal(stmt, item.get("notes"), is_original, measured_metric)

        cur.execute(
            """INSERT INTO facts (subject_id, statement, is_original_claim, is_personal, trust_level, trust_rationale,
                                   provided_by, date_added, last_reviewed_at, notes, recheck_by, recheck_rationale, origin_path)
               VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?)""",
            (subject_id, stmt, is_original, is_personal, trust, item.get("trust_rationale"),
             TODAY, TODAY, item.get("notes"), item.get("recheck_by"), item.get("recheck_rationale"), origin_path)
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

        if measured_metric:
            cur.execute(
                """INSERT INTO fact_measurements (fact_id, measurement_id)
                   SELECT ?, m.id FROM measurements m JOIN metrics mt ON mt.id = m.metric_id WHERE mt.key = ?""",
                (fact_id, measured_metric)
            )

        inserted += 1

    con.commit()
    print(f"[04_ingest_facts] inserted {inserted}, skipped {skipped} (bad shape)")
    if bad_citekeys:
        print(f"  WARNING -- citekeys referenced but not found in sources: {sorted(bad_citekeys)}")
    print("  facts by trust_level:", dict(cur.execute("SELECT trust_level, COUNT(*) FROM facts GROUP BY trust_level").fetchall()))
    print("  facts by is_personal:", dict(cur.execute("SELECT is_personal, COUNT(*) FROM facts GROUP BY is_personal").fetchall()))


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
