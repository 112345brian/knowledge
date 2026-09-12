"""Ingest every structured, repeatable numeric reading into `measurements`:
DEXA (both scans, full field set), bloodwork (verified + the two flagged
unlocated CRP/ESR readings), manual tape measurements, computed strength
checkpoints, then the vault's daily/weekly summary tables and full raw logs
(per-set training, per-meal/per-food nutrition, Fitbit measurements,
micronutrients). Run after 01_seed_sources.py (needs its citekeys to exist).

Source: <BODYBUILDING_VAULT>/bodybuilding.db
"""
import sqlite3, os, re

VAULT_DB = os.path.expanduser("<BODYBUILDING_VAULT>/bodybuilding.db")
VAULT = "<BODYBUILDING_VAULT>"
TODAY = "2026-09-11"


def get_source_id(cur, citekey):
    row = cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
    if not row:
        raise RuntimeError(f"source citekey not found: {citekey} (run 01_seed_sources.py first)")
    return row[0]


def get_or_create_source(cur, citekey, name, description, origin_path=None):
    row = cur.execute("SELECT id FROM sources WHERE citekey = ?", (citekey,)).fetchone()
    if row:
        return row[0]
    cur.execute("""INSERT INTO sources (citekey, name, source_type, author, publisher, url, published_date, retrieved_date, description, origin_path)
        VALUES (?, ?, 'primary', 'user', NULL, NULL, NULL, ?, ?, ?)""", (citekey, name, TODAY, description, origin_path))
    return cur.lastrowid


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower().strip()).strip("_")


STALE_NOTE = "Synced into knowledge.db as of 2026-09-11 -- a snapshot of a live, actively-updated vault table, not a re-syncing link."


def run(con):
    vault = sqlite3.connect(VAULT_DB)
    vault.row_factory = sqlite3.Row
    cur = con.cursor()
    inserted = 0

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
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES (?, ?, ?, ?, ?, ?, 'verified', 'Direct clinical DEXA measurement (BodySpec), read from the vault''s own structured bodycomp_dexa table.', ?, ?)""",
                (subject, metric, val, unit, scan_date, source_id, TODAY, note))
            inserted += 1
        ffm = (scan["lean_tissue_lbs"] or 0) + (scan["bmc_lbs"] or 0)
        cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
            VALUES ('body-composition', 'fat_free_mass_lb', ?, 'lb', ?, ?, 'verified', 'Direct clinical DEXA measurement (BodySpec).', ?, '= lean_tissue_lb + bone_mineral_content_lb, per the vault''s own v_dexa view.')""",
            (ffm, scan_date, source_id, TODAY))
        appendicular = (scan["arms_lean_lbs"] or 0) + (scan["legs_lean_lbs"] or 0)
        cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
            VALUES ('body-composition', 'appendicular_lean_lb', ?, 'lb', ?, ?, 'verified', 'Direct clinical DEXA measurement (BodySpec).', ?, '= arms_lean_lb + legs_lean_lb.')""",
            (appendicular, scan_date, source_id, TODAY))
        height_m = (scan["height_in"] or 0) * 0.0254
        if height_m:
            lmi = (scan["lean_tissue_lbs"] or 0) * 0.45359237 / (height_m ** 2)
            almi = appendicular * 0.45359237 / (height_m ** 2)
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('body-composition', 'lmi', ?, 'kg/m²', ?, ?, 'verified', 'Lean mass index, derived from the DEXA scan (lean soft tissue / height², bone excluded).', ?, NULL)""",
                (round(lmi, 2), scan_date, source_id, TODAY))
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('body-composition', 'almi', ?, 'kg/m²', ?, ?, 'verified', 'Appendicular lean mass index, derived from the DEXA scan.', ?, NULL)""",
                (round(almi, 2), scan_date, source_id, TODAY))
            inserted += 2
        inserted += 2

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
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('hormones-endocrinology', ?, ?, ?, ?, ?, 'unverified', 'Value is carried forward from prior vault notes; the vault''s own audit could not locate the underlying lab report for it.', ?, ?)""",
                (metric, row["value"], row["unit"], row["date"], src_unlocated, TODAY, " ".join(note_parts)))
        else:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes, recheck_by, recheck_rationale)
                VALUES ('hormones-endocrinology', ?, ?, ?, ?, ?, 'verified', 'Direct primary lab result (Labcorp), read from the vault''s own structured bloodwork table.', ?, ?, ?, ?)""",
                (metric, row["value"], row["unit"], row["date"], labcorp, TODAY, " ".join(note_parts), recheck_by, recheck_rationale))
        inserted += 1

    # ---------------- Manual tape measurements ----------------
    for row in vault.execute("SELECT * FROM body_measurements"):
        for col in ("neck_in", "shoulders_in", "chest_in", "upper_arm_in", "forearm_in", "waist_in",
                    "hips_in", "thigh_in", "calf_in", "wrist_in", "ankle_in"):
            val = row[col]
            if val is None:
                continue
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added)
                VALUES ('body-composition', ?, ?, 'in', ?, ?, 'high', 'Self-measured with a tape measure -- prone to placement/tension variance between sessions, but a direct measurement.', ?)""",
                (col, val, row["date"], src_tape, TODAY))
            inserted += 1

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
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes, recheck_by, recheck_rationale)
                VALUES ('strength-progression-norms', ?, ?, 'lb', ?, ?, 'high', 'Auto-computed estimated 1RM from logged training sets, not a tested single-rep max.', ?, ?, ?, ?)""",
                (f"{lift}_e1rm_lb", e1rm, as_of, src_strength, TODAY, f"checkpoint as of {latest['date']} snapshot", recheck_by, recheck_rationale))
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('strength-progression-norms', ?, ?, 'ratio', ?, ?, 'high', 'Bodyweight ratio derived from the e1RM checkpoint and same-day bodyweight.', ?, ?)""",
                (ratio_col, latest[ratio_col], as_of, src_strength, TODAY, f"checkpoint as of {latest['date']} snapshot"))
            inserted += 2

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
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added)
                VALUES (?, ?, ?, ?, ?, ?, 'verified', 'Self-logged via MacroFactor, compiled into the vault''s own structured daily table.', ?)""",
                (subject, metric, row[col], unit, row["date"], src_nutrition, TODAY))
            inserted += 1

    for row in vault.execute("SELECT * FROM scale_readings"):
        if row["weight_lbs"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added)
                VALUES ('body-composition', 'scale_weight_lb', ?, 'lb', ?, ?, 'high', 'Manual/compiled scale reading.', ?)""",
                (row["weight_lbs"], row["date"], src_scale, TODAY)); inserted += 1
        if row["body_fat_pct"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('body-composition', 'scale_bodyfat_pct', ?, '%', ?, ?, 'low', 'Bioimpedance-scale body-fat estimate, not DEXA.', ?, 'Not DEXA.')""",
                (row["body_fat_pct"], row["date"], src_scale, TODAY)); inserted += 1

    for row in vault.execute("SELECT * FROM renpho_scale_readings"):
        if row["weight_lbs"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added)
                VALUES ('body-composition', 'scale_weight_lb', ?, 'lb', ?, ?, 'high', 'RENPHO scale weight -- accurate to ~0.2 lb even though its body-composition estimates are not.', ?)""",
                (row["weight_lbs"], row["date"], src_renpho, TODAY)); inserted += 1
        if row["body_fat_pct"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('body-composition', 'scale_bodyfat_pct', ?, '%', ?, ?, 'low', 'RENPHO bioimpedance estimate, found 14.3 points high vs. DEXA on a same-day comparison.', ?, 'Not DEXA.')""",
                (row["body_fat_pct"], row["date"], src_renpho, TODAY)); inserted += 1
        if row["fat_free_mass_lbs"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('body-composition', 'scale_fat_free_mass_lb', ?, 'lb', ?, ?, 'low', 'RENPHO bioimpedance estimate, found 22.4 lb high vs. DEXA on a same-day comparison.', ?, 'Not DEXA.')""",
                (row["fat_free_mass_lbs"], row["date"], src_renpho, TODAY)); inserted += 1

    cols = [d[0] for d in vault.execute("SELECT * FROM muscle_volume_weekly LIMIT 1").description if d[0] != "date"]
    for row in vault.execute("SELECT * FROM muscle_volume_weekly"):
        for col in cols:
            if row[col] is None:
                continue
            muscle = col.replace("_sets", "")
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('training-volume-hypertrophy', ?, ?, 'sets/week', ?, ?, 'verified', 'Computed directly from logged training sets by the vault''s own volume script.', ?, ?)""",
                (f"{muscle}_sets_per_week", row[col], row["date"], src_volume, TODAY, f"Muscle: {muscle}"))
            inserted += 1

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
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('strength-progression-norms', ?, ?, ?, ?, ?, 'verified', 'Self-logged training set (Liftosaur/MacroFactor).', ?, ?)""",
                (f"set_{ex}_weight_lb", row["completed_weight"], row["weight_unit"] or "lb", row["date"], src_sets, TODAY, note)); inserted += 1
        if row["completed_reps"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('strength-progression-norms', ?, ?, 'reps', ?, ?, 'verified', 'Self-logged training set (Liftosaur/MacroFactor).', ?, ?)""",
                (f"set_{ex}_reps", row["completed_reps"], row["date"], src_sets, TODAY, note)); inserted += 1
        if row["rir"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('strength-progression-norms', ?, ?, 'RIR', ?, ?, 'verified', 'Self-logged training set (Liftosaur/MacroFactor).', ?, ?)""",
                (f"set_{ex}_rir", row["rir"], row["date"], src_sets, TODAY, note)); inserted += 1

    for row in vault.execute("SELECT * FROM jefit_exercise_sets"):
        ex = slug(row["exercise"] or "unknown_exercise")
        note = f"Exercise: {row['exercise']}"
        if row["weight"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('strength-progression-norms', ?, ?, ?, ?, ?, 'verified', 'Self-logged training set (Jefit).', ?, ?)""",
                (f"set_{ex}_weight_lb", row["weight"], row["weight_unit"] or "lb", row["date"], src_jefit, TODAY, note)); inserted += 1
        if row["reps"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('strength-progression-norms', ?, ?, 'reps', ?, ?, 'verified', 'Self-logged training set (Jefit).', ?, ?)""",
                (f"set_{ex}_reps", row["reps"], row["date"], src_jefit, TODAY, note)); inserted += 1

    MFP_M_COLS = [("weight_lbs", "scale_weight_lb", "lb"), ("fitbit_body_fat_pct", "fitbit_bodyfat_pct", "%"),
                  ("fitbit_steps", "fitbit_steps", "steps"), ("fitbit_sleep_minutes", "fitbit_sleep_minutes", "min")]
    for row in vault.execute("SELECT * FROM mfp_measurements"):
        for col, metric, unit in MFP_M_COLS:
            if row[col] is None:
                continue
            trust = "unverified" if metric == "fitbit_bodyfat_pct" else "high"
            rationale = ("Wrist-wearable body-fat estimate -- generally unreliable, not corroborated against DEXA."
                         if metric == "fitbit_bodyfat_pct" else "Fitbit-synced measurement via MyFitnessPal.")
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added)
                VALUES ('body-composition', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (metric, row[col], unit, row["date"], src_mfpm, trust, rationale, TODAY)); inserted += 1

    for row in vault.execute("SELECT * FROM mfp_exercise_log"):
        note = f"Activity: {row['exercise']} ({row['type']})" if row["exercise"] else None
        if row["exercise_calories"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('nutrition-energy-balance', 'cardio_calories_kcal', ?, 'kcal', ?, ?, 'high', 'Self-logged/app-estimated exercise calories via MyFitnessPal.', ?, ?)""",
                (row["exercise_calories"], row["date"], src_mfpe, TODAY, note)); inserted += 1
        if row["exercise_minutes"] is not None:
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('nutrition-energy-balance', 'cardio_minutes', ?, 'min', ?, ?, 'high', 'Self-logged exercise duration via MyFitnessPal.', ?, ?)""",
                (row["exercise_minutes"], row["date"], src_mfpe, TODAY, note)); inserted += 1

    MEAL_COLS = ["calories_kcal", "fat_g", "saturated_fat_g", "carbs_g", "fiber_g", "sugar_g", "protein_g",
                 "sodium_mg", "potassium_mg", "cholesterol_mg", "vitamin_a", "vitamin_c", "calcium", "iron"]
    for row in vault.execute("SELECT * FROM mfp_nutrition_log"):
        for col in MEAL_COLS:
            if row[col] is None:
                continue
            unit = "mg" if col.endswith("_mg") else ("g" if col.endswith("_g") else ("kcal" if col.endswith("_kcal") else "unit"))
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('nutrition-energy-balance', ?, ?, ?, ?, ?, 'verified', 'Self-logged per-meal nutrition via MyFitnessPal.', ?, ?)""",
                (f"meal_{col}", row[col], unit, row["date"], src_mfpn, TODAY, f"Meal: {row['meal']}")); inserted += 1

    FOOD_COLS = [("calories_kcal", "food_calories_kcal", "kcal"), ("fat_g", "food_fat_g", "g"),
                 ("carbs_g", "food_carbs_g", "g"), ("protein_g", "food_protein_g", "g"), ("alcohol_g", "food_alcohol_g", "g")]
    for row in vault.execute("SELECT * FROM nutrition_food_log"):
        note = f"Food: {row['food_name']}" + (f" ({row['serving_qty']} {row['serving_size']})" if row["serving_size"] else "")
        for col, metric, unit in FOOD_COLS:
            if row[col] is None:
                continue
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added, notes)
                VALUES ('nutrition-energy-balance', ?, ?, ?, ?, ?, 'verified', 'Self-logged individual food item via MacroFactor.', ?, ?)""",
                (metric, row[col], unit, row["date"], src_food, TODAY, note)); inserted += 1

    micro_cols = [d[0] for d in vault.execute("SELECT * FROM micronutrients LIMIT 1").description if d[0] not in ("date", "source_file")]
    for row in vault.execute("SELECT * FROM micronutrients"):
        for col in micro_cols:
            if row[col] is None:
                continue
            m = re.match(r"^(.*)_([a-z]+)$", col)
            unit = m.group(2) if m else "unit"
            cur.execute("""INSERT INTO measurements (subject, metric, value, unit, measured_at, source_id, trust_level, trust_rationale, date_added)
                VALUES ('nutrition-energy-balance', ?, ?, ?, ?, ?, 'verified', 'Self-logged, compiled from MacroFactor daily micronutrient export.', ?)""",
                (col, row[col], unit, row["date"], src_micro, TODAY)); inserted += 1

    con.commit()
    vault.close()
    print(f"[03_ingest_measurements] inserted {inserted} measurement rows")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
