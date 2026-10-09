"""A synthetic world for the whole ingest pipeline (steps 01-12): a private data dir, a vault with source
notes and a bodybuilding.db, a concerts CSV, an RYM export and a Last.fm scrobble dump, all invented here.

`build_world(root)` writes the inputs and returns the env for a subprocess; `run_pipeline(root)` runs every
step through build.build() in a fresh interpreter and returns a normalized dump of every table. The golden
file tests/golden/pipeline_dump.json is that dump from the code BEFORE the ports-and-adapters refactor of
the pipeline, so tests/test_pipeline_golden.py fails if a refactor changes what the pipeline ingests.
"""
import csv
import json
import os
import sqlite3
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VAULT_SCHEMA = {
    "bodycomp_dexa": "date, provider, total_mass_lbs, body_fat_pct, fat_mass_lbs, lean_tissue_lbs, bmc_lbs, rmr_kcal, "
                     "vat_mass_lbs, bmd_total, bmd_z_score, arms_lean_lbs, legs_lean_lbs, height_in",
    "bloodwork": "date, panel, analyte, value, unit, ref_low, ref_high, flag, fasting, on_medication, source_doc, notes",
    "body_measurements": "date, neck_in, shoulders_in, chest_in, upper_arm_in, forearm_in, waist_in, hips_in, thigh_in, calf_in, wrist_in, ankle_in",
    "strength_checkpoints": "date, squat_lbs, squat_bw_ratio, squat_as_of, bench_lbs, bench_bw_ratio, bench_as_of, "
                            "deadlift_lbs, deadlift_bw_ratio, deadlift_as_of, note",
    "nutrition_daily": "date, expenditure_kcal, trend_weight_lbs, weight_lbs, calories_kcal, protein_g, fat_g, carbs_g",
    "scale_readings": "date, weight_lbs, body_fat_pct, source",
    "renpho_scale_readings": "date, weight_lbs, body_fat_pct, fat_free_mass_lbs",
    "muscle_volume_weekly": "date, chest_sets, quads_sets, front_delts_sets",
    "workout_sets": "date, exercise, completed_weight, weight_unit, completed_reps, rir, is_warmup, notes",
    "jefit_exercise_sets": "date, exercise, weight, reps, weight_unit",
    "mfp_measurements": "date, fitbit_body_fat_pct, fitbit_steps, fitbit_sleep_minutes, weight_lbs",
    "mfp_exercise_log": "date, exercise, type, exercise_calories, exercise_minutes",
    "mfp_nutrition_log": "date, meal, calories_kcal, fat_g, saturated_fat_g, carbs_g, fiber_g, sugar_g, protein_g, sodium_mg, "
                         "potassium_mg, cholesterol_mg, vitamin_a, vitamin_c, calcium, iron",
    "nutrition_food_log": "date, time, food_name, serving_size, serving_qty, calories_kcal, fat_g, carbs_g, protein_g, alcohol_g",
    "micronutrients": "date, iron_mg, vitamin_c_mg, source_file",
}
VAULT_ROWS = {
    "bodycomp_dexa": [("2025-11-15", "x", 150.0, 20.0, 30.0, 110.0, 6.0, 1800, 1.0, 1.2, 0.5, 20.0, 40.0, 70.0),
                      ("2026-06-17", "x", 160.0, 18.0, 29.0, 125.0, 6.5, 1900, 0.9, 1.3, None, 22.0, 42.0, 70.0)],
    "bloodwork": [("2025-01-24", "p", "FSH", 9.9, "mIU/mL", 1.5, 12.4, "H", 1, None, "d", "high one"),
                  ("2025-01-24", "p", "LH", 4.0, "mIU/mL", 1.7, 8.6, None, 1, "x-med", "d", None),
                  ("2025-01-24", "p", "CRP", 1.1, "mg/L", None, None, None, 0, None, "d", "SOURCE DOCUMENT NOT FOUND"),
                  ("2025-01-24", "p", "Unmapped", 1.0, "u", None, None, None, 0, None, "d", None)],
    "body_measurements": [("2026-01-01", 15, 45, 40, 14, 11, 32, 38, 22, 15, 6.5, None)],
    "strength_checkpoints": [("2026-03-01", 300, 1.8, "2026-02-20", 200, 1.2, "2026-02-21", 400, 2.4, "2026-02-22",
                              "squat figure STALE")],
    "nutrition_daily": [("2026-02-01", 2500, 160.0, 161.0, 2400, 150, 70, 250), ("2026-02-02", None, None, 162.0, 2300, None, 60, 240)],
    "scale_readings": [("2026-02-02", 160.5, 18.5, "s"), ("2026-02-03", None, 19.0, "s")],
    "renpho_scale_readings": [("2026-02-04", 161.0, 21.0, 127.0)],
    "muscle_volume_weekly": [("2026-02-01", 12, 8, None), ("2026-02-08", 14, None, 3)],
    "workout_sets": [("2026-02-03", "Squat", 225, "lb", 5, 2, 0, None), ("2026-02-03", None, 135, None, 8, None, 1, "warm")],
    "jefit_exercise_sets": [("2022-02-03", "Curl", 30, 10, "lb"), ("2022-02-04", None, 35, 8, None)],
    "mfp_measurements": [("2026-02-04", 18.0, 9000, 420, 160.0)],
    "mfp_exercise_log": [("2026-02-05", "Run", "cardio", 300, 30), ("2026-02-06", None, None, None, 20)],
    "mfp_nutrition_log": [("2026-02-05", "Lunch", 600, 20, 5, 70, 8, 10, 30, 800, 900, 60, 10, 20, 5, 15)],
    "nutrition_food_log": [("2026-02-05", "12:30", "Rice", "1 cup", 1.0, 200, 1, 44, 4, 0), ("2026-02-05", "13:00", None, "x", 1, 10, 0, 1, 0, 0)],
    "micronutrients": [("2026-02-05", 12.5, 80.0, "f.csv")],
}

NOTE_TEMPLATE = """---
title: {title}
authors:
  - "{author1}"
  - [[{author2}]]
year: {year}
journal: {journal}
url: https://example.org/{key}
doi: 10.1000/{key}
pmid: 123{n}
source-type: {stype}
domain: {domain}
---
Body line.

> [!note] skip this callout
> The first real quote of {key} is here.
"""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _entry(key, subject, statement, **kw):
    e = {"source_key": key, "subject": subject, "statement": statement, "trust_level": "medium",
         "is_original_claim": False, "is_personal": True, "date_added": "2026-09-11T00:00:00+00:00",
         "visibility": "private", "status": "active", "freshness": "recheck", "recheck_by": "2027-01-01"}
    e.update(kw)
    return e


def build_world(root, sources=("concerts", "ratings", "scrobbles", "measurements", "claims")):
    """Write every input under `root` and return the environment dict for a subprocess. `sources` is the
    CLIENT_SOURCES the world's local_paths.py enables (the inputs are always written, enabled or not)."""
    root = str(root)
    private = os.path.join(root, "knowledge-private")
    data = os.path.join(root, "data")
    dbdir = os.path.join(root, "db")
    vault = os.path.join(root, "vault")
    for d in (private, data, dbdir, os.path.join(vault, "sources"), os.path.join(vault, "harm-reduction")):
        os.makedirs(d, exist_ok=True)
    concerts = os.path.join(root, "concerts.csv")
    rym = os.path.join(root, "rym.csv")
    scrobbles = os.path.join(root, "scrobbles.json")
    _write(os.path.join(private, "local_paths.py"),
           f"KNOWLEDGE_DB_DIR = {dbdir!r}\nPRIVATE_DATA_DIR = {data!r}\nBODYBUILDING_VAULT = {vault!r}\n"
           f"HEALTH_DIR = {os.path.join(root, 'health')!r}\nCONCERTS_CSV = {concerts!r}\nRYM_EXPORT_CSV = {rym!r}\n"
           f"SCROBBLES_JSON = {scrobbles!r}\nCLIENT_SOURCES = {tuple(sources)!r}\n")

    # --- vault: source notes (02) and bodybuilding.db (03)
    notes = [("alpha2020", "primary-research", "Alpha Study", "Smith, J", "Doe, A", 2020, "Journal A", "training"),
             ("beta2021", "systematic-review", "Beta Review", "Lee, K", "Wu, B", 2021, "Journal B", "nutrition"),
             ("gamma2022", None, "Gamma Label", "Ng, P", "Roe, C", 2022, "Vendor", "supplements"),
             ("delta-meta-analysis", None, "Delta", "Kim, H", "Pak, D", 2023, "Journal D", "x")]
    for n, (key, stype, title, a1, a2, year, journal, domain) in enumerate(notes):
        text = NOTE_TEMPLATE.format(title=title, author1=a1, author2=a2, year=year, journal=journal, key=key, n=n,
                                    stype=stype or "", domain=domain)
        if stype is None:
            text = text.replace("source-type: \n", "")
        _write(os.path.join(vault, "sources", key + ".md"), text)
    _write(os.path.join(vault, "sources", "README.md"), "ignored\n")
    _write(os.path.join(vault, "sources", "nofront.md"), "no frontmatter here\n> a quote line that is long enough\n")
    con = sqlite3.connect(os.path.join(vault, "bodybuilding.db"))
    for table, cols in VAULT_SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
        for row in VAULT_ROWS.get(table, []):
            con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(row))})", row)
    con.commit()
    con.close()

    # --- private data
    manual = [{"citekey": ck, "name": ck, "source_type": "primary", "author": "Self; Other", "publisher": "P",
               "url": None, "published_date": "2026", "retrieved_date": "2026-09-11", "description": "d",
               "origin_path": "{VAULT}/x/" + ck + ".md"}
              for ck in ("bodyspec-dexa-2026-06-17", "bodyspec-dexa-2025-11-15", "labcorp-2025-01-24",
                         "manual-tape-measurements", "strength-checkpoints-script", "unlocated-crp-esr-notes")]
    _write(os.path.join(data, "manual_sources.json"), json.dumps(manual, indent=1))
    pilot = [_entry("p-1", "anabolic-steroids", "Pilot fact one about training.", trust_level="high", notes="Current State.md, more",
                    is_original_claim=True, source_citekey="alpha2020", source_locator="p. 1", source_quote="q",
                    freshness="no-decay", recheck_rationale="stable", recheck_by=None),
             _entry("p-2", "aas-legal", "He tracked 156.4 lb once.", notes="harm-reduction/AAS and the Law.md, x",
                    measurement_metric_link="weight_lb"),
             _entry("p-3", "training-recovery", "Volume Is the Variable.", notes="Volume Is the Variable, Not Frequency.md"),
             _entry("p-4", "nutrition", "Bad trust.", trust_level="bogus"),
             _entry("p-5", "bone", "Whole-body BMD Z-score fell from -0.6 to -0.9."),
             _entry("p-6", "hormones", "The FSH result in the most recent hormone panel was high."),
             _entry("p-7", "training", "Approximately 10 to 20 sets per muscle per week is enough.")]
    legacy = {k: v for k, v in _entry("x", "legacy", "A legacy fact without keys.").items() if k not in ("source_key", "freshness")}
    batch1 = [legacy, _entry("b-1", "sleep", "Visibility oddity.", visibility="weird"),
              _entry("b-2", "sleep", "Status oddity.", status="odd"),
              _entry("b-3", "sleep", "AAS Cardiovascular Risk.md origin", notes="AAS Cardiovascular Risk.md")]
    # the world's own vault hints (fact_hints.json): fixture names only, the same shape the author keeps privately
    _write(os.path.join(data, "fact_hints.json"), json.dumps({
        "fingerprints": [r"156\.4"], "personal_notes": ["Current State"],
        "note_folders": {"harm-reduction": ["AAS Cardiovascular Risk.md"]},
        "whole_titles": ["Volume Is the Variable, Not Frequency.md"]}, indent=1))
    _write(os.path.join(data, "pilot_facts.json"), json.dumps(pilot, indent=1))
    for i in range(1, 5):
        _write(os.path.join(data, f"facts_batch{i}.json"), json.dumps(batch1 if i == 1 else [], indent=1))
    general = [_entry("g-1", "coffee", "Coffee fact.", captured_via="cli", captured_at="2026-09-12T00:00:00+00:00",
                      session_id="s1", domain="general", source_citekey="beta2021"),
               _entry("g-2", "coffee", "Mentions Zorblax by name.", is_personal=False),
               _entry("g-3", "tea", "Normal one.", visibility="normal", status="pending")]
    _write(os.path.join(data, "general_facts.json"), json.dumps(general, indent=1))
    _write(os.path.join(data, "measurements_snapshot.json"), json.dumps({"synced_at": "2026-09-11"}))
    _write(os.path.join(data, "privacy_rules.json"), json.dumps({"version": 1, "subject_tags": {"sleep": "private"}, "keywords": ["zorblax"]}))
    _write(os.path.join(data, "fact_revisions.jsonl"),
           json.dumps({"source_key": "g-3", "revision": 2, "changed_at": "2026-09-13T00:00:00+00:00", "changed_via": "cli",
                       "session_id": None, "change_reason": "approve", "statement": "Normal one.", "trust_level": "medium",
                       "trust_rationale": None, "status": "active", "visibility": "normal", "superseded_by": None,
                       "recheck_by": "2027-01-01", "recheck_rationale": None, "freshness": "recheck", "kind": "unclassified",
                       "valid_from": None, "valid_to": None, "applies_to": None, "notes": None}) + "\n")

    # --- music
    with open(concerts, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Concert", "Start Date", "End Date", "Location", "Notes"])
        w.writerows([["Act One", "2024-01-01", "", "The Venue, Town, ST", ""],
                     ["Act Two", "2024-02-02", "2024-02-03", "Coachella", "festival set"],
                     ["Coachella", "2024-04-12", "", "Indio, CA", ""],
                     ["Act Three", "2024-03-03", "", "Club", "opened for Big Band; fun"],
                     ["Act Four", "2024-05-05", "", "Some City", "a festival somewhere"],
                     ["", "2024-06-06", "", "x", ""], ["Act Five", "", "", "x", ""],
                     ["Bri", "2024-07-07", "", "Club, Town", ""]])
    with open(rym, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["RYM Album", " First Name", "Last Name", "First Name localized", " Last Name localized", "Title",
                    "Release_Date", "Rating"])
        w.writerows([["A1", "Freddie", "Gibbs &amp; Madlib", "", "", "Bandana &amp; Co", "2019", "9"],
                     ["A2", "x", "y", "Native", "Name", "Title Two", "2020", "7"],
                     ["A3", "", "", "", "", "Untitled", "2021", "5"],
                     ["A4", "The", "Ramones", "", "", "Rocket to Russia", "1977", "10"]])
    pages = [[{"date": {"uts": "1700000000"}, "artist": {"#text": "Madvillain"}, "name": "Accordion", "album": {"#text": "Madvillainy"}},
              {"artist": {"#text": "Madlib"}, "name": "now playing"},
              {"date": {"uts": "1700000100"}, "artist": {"#text": "madvillain"}, "name": "Accordion", "album": {"#text": "Madvillainy"}}],
             [{"date": {"uts": "1700000200"}, "artist": {"#text": "Freddie Gibbs, Madlib"}, "name": "Song", "album": {"#text": ""}},
              {"date": {"uts": "1700000300"}, "artist": {"#text": ""}, "name": "x"}]]
    _write(scrobbles, json.dumps(pages))
    return {**os.environ, "KNOWLEDGE_PRIVATE_DIR": private, "KNOWLEDGE_FROZEN_NOW": "2026-10-01T00:00:00+00:00", "PYTHONHASHSEED": "0"}


DUMP_SCRIPT = r"""
import json, os, re, sqlite3, sys
TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")  # CURRENT_TIMESTAMP defaults vary run to run
sys.path.insert(0, sys.argv[1])
import build
out = os.path.join(sys.argv[2], "pipeline.db")
build.build(out)
con = sqlite3.connect(out)
dump = {}
for (name, sql) in con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name"):
    if name.endswith(('_data', '_idx', '_docsize', '_config', '_content')) or name.startswith('sqlite_'):
        continue
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
    # file_mtime is the real mtime of a file the test just wrote, and build_info records the commit and the
    # interpreter, so these differ on every run or machine
    rows = [["<TS>" if isinstance(v, str) and TS.match(v) else ("<MTIME>" if cols[i] == "file_mtime" and v is not None else
                                                  "<ENV>" if name == "build_info" and cols[i] in ("code_commit", "private_commit", "python_version", "sqlite_version") and v is not None else v)
             for i, v in enumerate(r)] for r in con.execute(f'SELECT * FROM "{name}"')]
    rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
    dump[name] = {"columns": cols, "rows": rows}
print("@@DUMP@@" + json.dumps(dump, sort_keys=True, default=str))
"""


def run_pipeline(root, repo=REPO, sources=("concerts", "ratings", "scrobbles", "measurements", "claims")):
    """Run every build step in a fresh interpreter against the world under `root`; return the table dump."""
    env = build_world(root, sources)
    out = os.path.join(str(root), "out")
    os.makedirs(out, exist_ok=True)
    p = subprocess.run([sys.executable, "-c", DUMP_SCRIPT, repo, out], env=env, cwd=repo, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"pipeline failed:\n{p.stdout[-2000:]}\n{p.stderr[-3000:]}")
    text = p.stdout.split("@@DUMP@@", 1)[1]
    # the world lives under a temp dir: its path is not part of what the pipeline ingests
    for variant in {str(root), os.path.realpath(str(root))}:
        text = text.replace(json.dumps(variant)[1:-1], "<ROOT>")
    return json.loads(text)
