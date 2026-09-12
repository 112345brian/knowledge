"""Ingest every structured, repeatable numeric reading into `measurements`:
DEXA (both scans, full field set), bloodwork (verified + the two flagged
unlocated CRP/ESR readings), manual tape measurements, computed strength
checkpoints, then the vault's daily/weekly summary tables and full raw logs
(per-set training, per-meal/per-food nutrition, Fitbit measurements,
micronutrients). Run after 01_seed_sources.py (needs its citekeys to exist).

Subject and metric are resolved to subject_id/metric_id via get-or-create
helpers -- never written as repeated text on the measurements row itself.

Source: <BODYBUILDING_VAULT>/bodybuilding.db
"""
import sqlite3, os, re

VAULT_DB = os.path.expanduser("<BODYBUILDING_VAULT>/bodybuilding.db")
VAULT = "<BODYBUILDING_VAULT>"
TODAY = "2026-09-11"

# Curated labels/good-direction for the metrics worth a human-friendly name.
# Anything not listed here gets an auto-generated label (see humanize()) --
# that's expected and fine for the long tail of per-set/per-food/micronutrient
# metrics, which don't need hand-curation to be usable.
METRIC_META = {
    "weight_lb": ("Total weight", 1), "body_fat_pct": ("Body fat", -1), "fat_mass_lb": ("Fat mass", None),
    "lean_tissue_lb": ("Lean tissue", None), "fat_free_mass_lb": ("Fat-free mass", None),
    "bone_mineral_content_lb": ("Bone mineral content", None), "bmd_total_g_cm2": ("BMD (total)", None),
    "bmd_zscore": ("BMD Z-score", 1), "appendicular_lean_lb": ("Appendicular lean mass", None),
    "lmi": ("LMI", None), "almi": ("ALMI", None), "rmr_kcal": ("RMR", None), "vat_mass_lb": ("Visceral fat mass", -1),
    "lh_miu_ml": ("LH", None), "fsh_miu_ml": ("FSH", None), "testosterone_total_ng_dl": ("Testosterone (total)", None),
    "testosterone_free_pg_ml": ("Testosterone (free)", None), "estradiol_pg_ml": ("Estradiol", None),
    "crp_mg_l": ("CRP", None), "esr_mm_hr": ("ESR", None),
    "neck_in": ("Neck", None), "shoulders_in": ("Shoulders", None), "chest_in": ("Chest", None),
    "upper_arm_in": ("Upper arm", None), "forearm_in": ("Forearm", None), "waist_in": ("Waist", -1),
    "hips_in": ("Hips", None), "thigh_in": ("Thigh", None), "calf_in": ("Calf", None),
    "wrist_in": ("Wrist", None), "ankle_in": ("Ankle", None),
    "squat_e1rm_lb": ("Squat e1RM", 1), "bench_e1rm_lb": ("Bench e1RM", 1), "deadlift_e1rm_lb": ("Deadlift e1RM", 1),
    "squat_bw_ratio": ("Squat : BW", 1), "bench_bw_ratio": ("Bench : BW", 1), "deadlift_bw_ratio": ("Deadlift : BW", 1),
    "scale_weight_lb": ("Scale weight", None), "scale_bodyfat_pct": ("Scale body fat %", -1),
    "scale_fat_free_mass_lb": ("Scale fat-free mass", None), "trend_weight_lb": ("Trend weight", None),
    "logged_calories_kcal": ("Logged calories", None), "logged_protein_g": ("Logged protein", None),
    "logged_fat_g": ("Logged fat", None), "logged_carbs_g": ("Logged carbs", None),
    "modeled_expenditure_kcal": ("Modeled expenditure", None),
    "fitbit_bodyfat_pct": ("Fitbit body fat %", None), "fitbit_steps": ("Fitbit steps", None),
    "fitbit_sleep_minutes": ("Fitbit sleep", None), "cardio_calories_kcal": ("Cardio calories", None),
    "cardio_minutes": ("Cardio minutes", None),
}


def humanize(key):
    return key.replace("_", " ").strip().capitalize()


_subject_cache = {}
_metric_cache = {}


def get_or_create_subject(cur, name):
    if name in _subject_cache:
        return _subject_cache[name]
    row = cur.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
    if not row:
        cur.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
        sid = cur.lastrowid
    else:
        sid = row[0]
    _subject_cache[name] = sid
    return sid


def get_or_create_metric(cur, key, unit):
    if key in _metric_cache:
        return _metric_cache[key]
    row = cur.execute("SELECT id FROM metrics WHERE key = ?", (key,)).fetchone()
    if not row:
        label, good_direction = METRIC_META.get(key, (humanize(key), None))
        cur.execute("INSERT INTO metrics (key, label, unit, good_direction) VALUES (?, ?, ?, ?)",
                    (key, label, unit, good_direction))
        mid = cur.lastrowid
    else:
        mid = row[0]
    _metric_cache[key] = mid
    return mid


def insert_measurement(cur, *, subject, metric, value, unit, measured_at, source_id, trust_level,
                        trust_rationale, notes=None, recheck_by=None, recheck_rationale=None):
    subject_id = get_or_create_subject(cur, subject)
    metric_id = get_or_create_metric(cur, metric, unit)
    cur.execute(
        """INSERT INTO measurements (subject_id, metric_id, value, measured_at, source_id, trust_level,
                                      trust_rationale, date_added, notes, recheck_by, recheck_rationale)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (subject_id, metric_id, value, measured_at, source_id, trust_level, trust_rationale,
         TODAY, notes, recheck_by, recheck_rationale),
    )


def get_source_id(cur, citekey):
    row = cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
    if not row:
        raise RuntimeError(f"source citekey not found: {citekey} (run 01_seed_sources.py first)")
    return row[0]


def get_or_create_source(cur, citekey, name, description, origin_path=None):
    row = cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
    if row:
        return row[0]
    cur.execute("""INSERT INTO sources (citekey, name, source_type, publisher, url, published_date, retrieved_date, description, origin_path)
        VALUES (?, ?, 'primary', NULL, NULL, NULL, ?, ?, ?)""", (citekey, name, TODAY, description, origin_path))
    source_id = cur.lastrowid
    author_id = cur.execute("SELECT id FROM authors WHERE name = 'user'").fetchone()
    if not author_id:
        cur.execute("INSERT INTO authors (name) VALUES ('user')")
        author_id = (cur.lastrowid,)
    cur.execute("INSERT OR IGNORE INTO source_authors (source_id, author_id) VALUES (?, ?)", (source_id, author_id[0]))
    return source_id


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower().strip()).strip("_")


STALE_NOTE = "Synced into knowledge.db as of 2026-09-11 -- a snapshot of a live, actively-updated vault table, not a re-syncing link."


def run(con):
    vault = sqlite3.connect(VAULT_DB)
    vault.row_factory = sqlite3.Row
    cur = con.cursor()
    _subject_cache.clear()
    _metric_cache.clear()
    inserted_before = cur.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]

    dexa_2026 = get_source_id(cur, "bodyspec-dexa-2026-06-17")
    dexa_2025 = get_source_id(cur, "bodyspec-dexa-2025-11-15")
    labcorp = get_source_id(cur, "labcorp-2025-01-24")
    src_tape = get_source_id(cur, "manual-tape-measurements")
    src_strength = get_source_id(cur, "strength-checkpoints-script")
    src_unlocated = get_source_id(cur, "unlocated-crp-esr-notes")

    # ---------------- DEXA: both scans, full field set from bodycomp_dexa ----------------
    DEXA_ROWS = [
        ("weight_lb", "lb", "total_mass_lbs", "body-composition", None),
        ("body_fat_pct", "%", "body_fat_pct", "body-composition", None),
        ("fat_mass_lb", "lb", "fat_mass_lbs", "body-composition", None),
        ("lean_tissue_lb", "lb", "lean_tissue_lbs", "body-composition", None),
        ("bone_mineral_content_lb", "lb", "bmc_lbs", "body-composition", None),
        ("bmd_total_g_cm2", "g/cm²", "bmd_total", "bone-density", None),
        ("bmd_zscore", "z", "bmd_z_score", "bone-density", "Age- and sex-matched Z-score."),
        ("rmr_kcal", "kcal/day", "rmr_kcal", "nutrition-energy-balance", "Resting metabolic rate as measured/estimated by the DEXA provider."),
        ("vat_mass_lb", "lb", "vat_mass_lbs", "body-composition", "Visceral adipose tissue mass."),
    ]
    for scan_date, source_id in [("2025-11-15", dexa_2025), ("2026-06-17", dexa_2026)]:
        scan = vault.execute("SELECT * FROM bodycomp_dexa WHERE date = ?", (scan_date,)).fetchone()
        for metric, unit, col, subject, note in DEXA_ROWS:
            val = scan[col]
            if val is None:
                continue
            insert_measurement(cur, subject=subject, metric=metric, value=val, unit=unit, measured_at=scan_date,
                                source_id=source_id, trust_level="verified",
                                trust_rationale="Direct clinical DEXA measurement (BodySpec), read from the vault's own structured bodycomp_dexa table.",
                                notes=note)
        ffm = (scan["lean_tissue_lbs"] or 0) + (scan["bmc_lbs"] or 0)
        insert_measurement(cur, subject="body-composition", metric="fat_free_mass_lb", value=ffm, unit="lb",
                            measured_at=scan_date, source_id=source_id, trust_level="verified",
                            trust_rationale="Direct clinical DEXA measurement (BodySpec).",
                            notes="= lean_tissue_lb + bone_mineral_content_lb, per the vault's own v_dexa view.")
        appendicular = (scan["arms_lean_lbs"] or 0) + (scan["legs_lean_lbs"] or 0)
        insert_measurement(cur, subject="body-composition", metric="appendicular_lean_lb", value=appendicular, unit="lb",
                            measured_at=scan_date, source_id=source_id, trust_level="verified",
                            trust_rationale="Direct clinical DEXA measurement (BodySpec).",
                            notes="= arms_lean_lb + legs_lean_lb.")
        height_m = (scan["height_in"] or 0) * 0.0254
        if height_m:
            lmi = (scan["lean_tissue_lbs"] or 0) * 0.45359237 / (height_m ** 2)
            almi = appendicular * 0.45359237 / (height_m ** 2)
            insert_measurement(cur, subject="body-composition", metric="lmi", value=round(lmi, 2), unit="kg/m²",
                                measured_at=scan_date, source_id=source_id, trust_level="verified",
                                trust_rationale="Lean mass index, derived from the DEXA scan (lean soft tissue / height², bone excluded).")
            insert_measurement(cur, subject="body-composition", metric="almi", value=round(almi, 2), unit="kg/m²",
                                measured_at=scan_date, source_id=source_id, trust_level="verified",
                                trust_rationale="Appendicular lean mass index, derived from the DEXA scan.")

    # ---------------- Bloodwork ----------------
    BLOOD_METRIC_MAP = {"LH": "lh_miu_ml", "FSH": "fsh_miu_ml", "Testosterone total": "testosterone_total_ng_dl",
                         "Testosterone free (direct)": "testosterone_free_pg_ml", "Estradiol": "estradiol_pg_ml",
                         "CRP": "crp_mg_l", "Sed rate (ESR)": "esr_mm_hr"}
    for row in vault.execute("SELECT * FROM bloodwork"):
        metric = BLOOD_METRIC_MAP.get(row["analyte"])
        if not metric:
            continue
        unlocated = "SOURCE DOCUMENT NOT FOUND" in (row["notes"] or "")
        note_parts = []
        if row["ref_low"] is not None and row["ref_high"] is not None:
            note_parts.append(f"Reference range {row['ref_low']}-{row['ref_high']} {row['unit']}.")
        if row["flag"]:
            note_parts.append(f"Flagged {row['flag']}.")
        if row["notes"] and not unlocated:
            note_parts.append(row["notes"])
        if row["on_medication"]:
            note_parts.append(f"Drawn while on {row['on_medication']}.")

        recheck_by = "as soon as possible" if metric == "fsh_miu_ml" else None
        recheck_rationale = ("Panel is now over 19 months old and predates a documented 13 lb weight loss; the "
                              "vault treats repeating this panel as its top-priority action item, since exogenous "
                              "testosterone use before a redraw would permanently confound future interpretation "
                              "of the unexplained FSH result.") if metric == "fsh_miu_ml" else None

        if unlocated:
            insert_measurement(cur, subject="hormones-endocrinology", metric=metric, value=row["value"], unit=row["unit"],
                                measured_at=row["date"], source_id=src_unlocated, trust_level="unverified",
                                trust_rationale="Value is carried forward from prior vault notes; the vault's own audit could not locate the underlying lab report for it.",
                                notes=" ".join(note_parts))
        else:
            insert_measurement(cur, subject="hormones-endocrinology", metric=metric, value=row["value"], unit=row["unit"],
                                measured_at=row["date"], source_id=labcorp, trust_level="verified",
                                trust_rationale="Direct primary lab result (Labcorp), read from the vault's own structured bloodwork table.",
                                notes=" ".join(note_parts), recheck_by=recheck_by, recheck_rationale=recheck_rationale)

    # ---------------- Manual tape measurements ----------------
    for row in vault.execute("SELECT * FROM body_measurements"):
        for col in ("neck_in", "shoulders_in", "chest_in", "upper_arm_in", "forearm_in", "waist_in",
                    "hips_in", "thigh_in", "calf_in", "wrist_in", "ankle_in"):
            val = row[col]
            if val is None:
                continue
            insert_measurement(cur, subject="body-composition", metric=col, value=val, unit="in",
                                measured_at=row["date"], source_id=src_tape, trust_level="high",
                                trust_rationale="Self-measured with a tape measure -- prone to placement/tension variance between sessions, but a direct measurement.")

    # ---------------- Strength checkpoints (latest snapshot only) ----------------
    latest = vault.execute("SELECT * FROM strength_checkpoints ORDER BY date DESC LIMIT 1").fetchone()
    if latest:
        for lift, ratio_col, as_of_col in [("squat", "squat_bw_ratio", "squat_as_of"),
                                            ("bench", "bench_bw_ratio", "bench_as_of"),
                                            ("deadlift", "deadlift_bw_ratio", "deadlift_as_of")]:
            e1rm = latest[f"{lift}_lbs"]
            as_of = latest[as_of_col]
            if e1rm is None:
                continue
            stale = "STALE" in (latest["note"] or "") and lift in (latest["note"] or "")
            recheck_by = "as soon as the next working set of this lift is logged" if stale else None
            recheck_rationale = ("The vault's own scripts/strength_checkpoint.py flags this specific figure "
                                  f"stale as of the {latest['date']} snapshot.") if stale else None
            insert_measurement(cur, subject="strength-progression-norms", metric=f"{lift}_e1rm_lb", value=e1rm, unit="lb",
                                measured_at=as_of, source_id=src_strength, trust_level="high",
                                trust_rationale="Auto-computed estimated 1RM from logged training sets, not a tested single-rep max.",
                                notes=f"checkpoint as of {latest['date']} snapshot", recheck_by=recheck_by, recheck_rationale=recheck_rationale)
            insert_measurement(cur, subject="strength-progression-norms", metric=ratio_col, value=latest[ratio_col], unit="ratio",
                                measured_at=as_of, source_id=src_strength, trust_level="high",
                                trust_rationale="Bodyweight ratio derived from the e1RM checkpoint and same-day bodyweight.",
                                notes=f"checkpoint as of {latest['date']} snapshot")

    # ---------------- Daily/weekly summary tables ----------------
    src_nutrition = get_or_create_source(cur, "vault-db-nutrition-daily", "bodybuilding.db table nutrition_daily (daily logged nutrition + weight)",
        "Compiled daily rollup from MacroFactor exports, loaded into the vault's own bodybuilding.db. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#nutrition_daily")
    src_scale = get_or_create_source(cur, "vault-db-scale-readings", "bodybuilding.db table scale_readings (manual/compiled scale weight log)",
        "Weight readings compiled from various exports and screenshots. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#scale_readings")
    src_renpho = get_or_create_source(cur, "vault-db-renpho-scale", "bodybuilding.db table renpho_scale_readings (RENPHO smart-scale export)",
        "RENPHO bioimpedance scale readings. The vault's own note found body-fat%/FFM biased 14-32 points off DEXA on the same day -- weight itself is accurate to ~0.2 lb. " + STALE_NOTE,
        f"{VAULT}/bodybuilding.db#renpho_scale_readings")
    src_volume = get_or_create_source(cur, "vault-db-muscle-volume-weekly", "bodybuilding.db table muscle_volume_weekly (computed training volume rollup)",
        "Per-muscle weekly set counts computed by the vault's own scripts. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#muscle_volume_weekly")

    NUTRITION_METRICS = [
        ("weight_lbs", "weight_lb", "lb", "body-composition"), ("trend_weight_lbs", "trend_weight_lb", "lb", "body-composition"),
        ("calories_kcal", "logged_calories_kcal", "kcal", "nutrition-energy-balance"), ("protein_g", "logged_protein_g", "g", "nutrition-energy-balance"),
        ("fat_g", "logged_fat_g", "g", "nutrition-energy-balance"), ("carbs_g", "logged_carbs_g", "g", "nutrition-energy-balance"),
        ("expenditure_kcal", "modeled_expenditure_kcal", "kcal", "nutrition-energy-balance"),
    ]
    for row in vault.execute("SELECT * FROM nutrition_daily"):
        for col, metric, unit, subject in NUTRITION_METRICS:
            if row[col] is None:
                continue
            insert_measurement(cur, subject=subject, metric=metric, value=row[col], unit=unit, measured_at=row["date"],
                                source_id=src_nutrition, trust_level="verified",
                                trust_rationale="Self-logged via MacroFactor, compiled into the vault's own structured daily table.")

    for row in vault.execute("SELECT * FROM scale_readings"):
        if row["weight_lbs"] is not None:
            insert_measurement(cur, subject="body-composition", metric="scale_weight_lb", value=row["weight_lbs"], unit="lb",
                                measured_at=row["date"], source_id=src_scale, trust_level="high",
                                trust_rationale="Manual/compiled scale reading.")
        if row["body_fat_pct"] is not None:
            insert_measurement(cur, subject="body-composition", metric="scale_bodyfat_pct", value=row["body_fat_pct"], unit="%",
                                measured_at=row["date"], source_id=src_scale, trust_level="low",
                                trust_rationale="Bioimpedance-scale body-fat estimate, not DEXA.", notes="Not DEXA.")

    for row in vault.execute("SELECT * FROM renpho_scale_readings"):
        if row["weight_lbs"] is not None:
            insert_measurement(cur, subject="body-composition", metric="scale_weight_lb", value=row["weight_lbs"], unit="lb",
                                measured_at=row["date"], source_id=src_renpho, trust_level="high",
                                trust_rationale="RENPHO scale weight -- accurate to ~0.2 lb even though its body-composition estimates are not.")
        if row["body_fat_pct"] is not None:
            insert_measurement(cur, subject="body-composition", metric="scale_bodyfat_pct", value=row["body_fat_pct"], unit="%",
                                measured_at=row["date"], source_id=src_renpho, trust_level="low",
                                trust_rationale="RENPHO bioimpedance estimate, found 14.3 points high vs. DEXA on a same-day comparison.", notes="Not DEXA.")
        if row["fat_free_mass_lbs"] is not None:
            insert_measurement(cur, subject="body-composition", metric="scale_fat_free_mass_lb", value=row["fat_free_mass_lbs"], unit="lb",
                                measured_at=row["date"], source_id=src_renpho, trust_level="low",
                                trust_rationale="RENPHO bioimpedance estimate, found 22.4 lb high vs. DEXA on a same-day comparison.", notes="Not DEXA.")

    cols = [d[0] for d in vault.execute("SELECT * FROM muscle_volume_weekly LIMIT 1").description if d[0] != "date"]
    for row in vault.execute("SELECT * FROM muscle_volume_weekly"):
        for col in cols:
            if row[col] is None:
                continue
            muscle = col.replace("_sets", "")
            insert_measurement(cur, subject="training-volume-hypertrophy", metric=f"{muscle}_sets_per_week", value=row[col],
                                unit="sets/week", measured_at=row["date"], source_id=src_volume, trust_level="verified",
                                trust_rationale="Computed directly from logged training sets by the vault's own volume script.",
                                notes=f"Muscle: {muscle}")

    # ---------------- Raw per-set / per-food / Fitbit / micronutrient logs ----------------
    src_sets = get_or_create_source(cur, "vault-db-workout-sets", "bodybuilding.db table workout_sets (per-set training log, 2024-12-12+)",
        "Per-set training log merged from Liftosaur and MacroFactor. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#workout_sets")
    src_jefit = get_or_create_source(cur, "vault-db-jefit-sets", "bodybuilding.db table jefit_exercise_sets (per-set training log, 2019-2024)",
        "Per-set training log exported from Jefit. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#jefit_exercise_sets")
    src_mfpm = get_or_create_source(cur, "vault-db-mfp-measurements", "bodybuilding.db table mfp_measurements (Fitbit-linked weight/sleep/steps, 2018-2026)",
        "Weight, body fat %, steps, sleep from Fitbit via MyFitnessPal. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#mfp_measurements")
    src_mfpe = get_or_create_source(cur, "vault-db-mfp-exercise-log", "bodybuilding.db table mfp_exercise_log (cardio/activity log)",
        "Cardio/activity logged via MyFitnessPal. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#mfp_exercise_log")
    src_mfpn = get_or_create_source(cur, "vault-db-mfp-nutrition-log", "bodybuilding.db table mfp_nutrition_log (per-meal macro/micro totals)",
        "Per-meal nutrition totals via MyFitnessPal, 2018-2026. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#mfp_nutrition_log")
    src_food = get_or_create_source(cur, "vault-db-nutrition-food-log", "bodybuilding.db table nutrition_food_log (individual logged food items)",
        "Individual food-item entries from MacroFactor. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#nutrition_food_log")
    src_micro = get_or_create_source(cur, "vault-db-micronutrients", "bodybuilding.db table micronutrients (daily micronutrient totals)",
        "Daily micronutrient totals compiled from MacroFactor exports. " + STALE_NOTE, f"{VAULT}/bodybuilding.db#micronutrients")

    for row in vault.execute("SELECT * FROM workout_sets"):
        ex = slug(row["exercise"] or "unknown_exercise")
        note = f"Exercise: {row['exercise']}" + (" (warmup)" if row["is_warmup"] else "")
        if row["completed_weight"] is not None:
            insert_measurement(cur, subject="strength-progression-norms", metric=f"set_{ex}_weight_lb", value=row["completed_weight"],
                                unit=row["weight_unit"] or "lb", measured_at=row["date"], source_id=src_sets, trust_level="verified",
                                trust_rationale="Self-logged training set (Liftosaur/MacroFactor).", notes=note)
        if row["completed_reps"] is not None:
            insert_measurement(cur, subject="strength-progression-norms", metric=f"set_{ex}_reps", value=row["completed_reps"],
                                unit="reps", measured_at=row["date"], source_id=src_sets, trust_level="verified",
                                trust_rationale="Self-logged training set (Liftosaur/MacroFactor).", notes=note)
        if row["rir"] is not None:
            insert_measurement(cur, subject="strength-progression-norms", metric=f"set_{ex}_rir", value=row["rir"],
                                unit="RIR", measured_at=row["date"], source_id=src_sets, trust_level="verified",
                                trust_rationale="Self-logged training set (Liftosaur/MacroFactor).", notes=note)

    for row in vault.execute("SELECT * FROM jefit_exercise_sets"):
        ex = slug(row["exercise"] or "unknown_exercise")
        note = f"Exercise: {row['exercise']}"
        if row["weight"] is not None:
            insert_measurement(cur, subject="strength-progression-norms", metric=f"set_{ex}_weight_lb", value=row["weight"],
                                unit=row["weight_unit"] or "lb", measured_at=row["date"], source_id=src_jefit, trust_level="verified",
                                trust_rationale="Self-logged training set (Jefit).", notes=note)
        if row["reps"] is not None:
            insert_measurement(cur, subject="strength-progression-norms", metric=f"set_{ex}_reps", value=row["reps"],
                                unit="reps", measured_at=row["date"], source_id=src_jefit, trust_level="verified",
                                trust_rationale="Self-logged training set (Jefit).", notes=note)

    MFP_M_COLS = [("weight_lbs", "scale_weight_lb", "lb"), ("fitbit_body_fat_pct", "fitbit_bodyfat_pct", "%"),
                  ("fitbit_steps", "fitbit_steps", "steps"), ("fitbit_sleep_minutes", "fitbit_sleep_minutes", "min")]
    for row in vault.execute("SELECT * FROM mfp_measurements"):
        for col, metric, unit in MFP_M_COLS:
            if row[col] is None:
                continue
            trust = "unverified" if metric == "fitbit_bodyfat_pct" else "high"
            rationale = ("Wrist-wearable body-fat estimate -- generally unreliable, not corroborated against DEXA."
                         if metric == "fitbit_bodyfat_pct" else "Fitbit-synced measurement via MyFitnessPal.")
            insert_measurement(cur, subject="body-composition", metric=metric, value=row[col], unit=unit,
                                measured_at=row["date"], source_id=src_mfpm, trust_level=trust, trust_rationale=rationale)

    for row in vault.execute("SELECT * FROM mfp_exercise_log"):
        note = f"Activity: {row['exercise']} ({row['type']})" if row["exercise"] else None
        if row["exercise_calories"] is not None:
            insert_measurement(cur, subject="nutrition-energy-balance", metric="cardio_calories_kcal", value=row["exercise_calories"],
                                unit="kcal", measured_at=row["date"], source_id=src_mfpe, trust_level="high",
                                trust_rationale="Self-logged/app-estimated exercise calories via MyFitnessPal.", notes=note)
        if row["exercise_minutes"] is not None:
            insert_measurement(cur, subject="nutrition-energy-balance", metric="cardio_minutes", value=row["exercise_minutes"],
                                unit="min", measured_at=row["date"], source_id=src_mfpe, trust_level="high",
                                trust_rationale="Self-logged exercise duration via MyFitnessPal.", notes=note)

    MEAL_COLS = ["calories_kcal", "fat_g", "saturated_fat_g", "carbs_g", "fiber_g", "sugar_g", "protein_g",
                 "sodium_mg", "potassium_mg", "cholesterol_mg", "vitamin_a", "vitamin_c", "calcium", "iron"]
    for row in vault.execute("SELECT * FROM mfp_nutrition_log"):
        for col in MEAL_COLS:
            if row[col] is None:
                continue
            unit = "mg" if col.endswith("_mg") else ("g" if col.endswith("_g") else ("kcal" if col.endswith("_kcal") else "unit"))
            insert_measurement(cur, subject="nutrition-energy-balance", metric=f"meal_{col}", value=row[col], unit=unit,
                                measured_at=row["date"], source_id=src_mfpn, trust_level="verified",
                                trust_rationale="Self-logged per-meal nutrition via MyFitnessPal.", notes=f"Meal: {row['meal']}")

    FOOD_COLS = [("calories_kcal", "food_calories_kcal", "kcal"), ("fat_g", "food_fat_g", "g"),
                 ("carbs_g", "food_carbs_g", "g"), ("protein_g", "food_protein_g", "g"), ("alcohol_g", "food_alcohol_g", "g")]
    for row in vault.execute("SELECT * FROM nutrition_food_log"):
        note = f"Food: {row['food_name']}" + (f" ({row['serving_qty']} {row['serving_size']})" if row["serving_size"] else "")
        for col, metric, unit in FOOD_COLS:
            if row[col] is None:
                continue
            insert_measurement(cur, subject="nutrition-energy-balance", metric=metric, value=row[col], unit=unit,
                                measured_at=row["date"], source_id=src_food, trust_level="verified",
                                trust_rationale="Self-logged individual food item via MacroFactor.", notes=note)

    micro_cols = [d[0] for d in vault.execute("SELECT * FROM micronutrients LIMIT 1").description if d[0] not in ("date", "source_file")]
    for row in vault.execute("SELECT * FROM micronutrients"):
        for col in micro_cols:
            if row[col] is None:
                continue
            m = re.match(r"^(.*)_([a-z]+)$", col)
            unit = m.group(2) if m else "unit"
            insert_measurement(cur, subject="nutrition-energy-balance", metric=col, value=row[col], unit=unit,
                                measured_at=row["date"], source_id=src_micro, trust_level="verified",
                                trust_rationale="Self-logged, compiled from MacroFactor daily micronutrient export.")

    con.commit()
    vault.close()
    inserted = cur.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] - inserted_before
    print(f"[03_ingest_measurements] inserted {inserted} measurement rows "
          f"({len(_subject_cache)} subjects, {len(_metric_cache)} metrics touched)")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
