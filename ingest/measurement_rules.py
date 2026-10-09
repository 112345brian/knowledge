"""Measurement ingest rules (step 03), domain: no vault db, no knowledge db.

How a row of the vault's own bodybuilding.db becomes `measurements`: which metric, unit, subject, trust level
and rationale each column gets, the derived readings (fat-free mass, appendicular lean mass, LMI/ALMI), the
stale-figure flags and the vault-db sources' descriptions. Every function takes plain row mappings and returns
`Measurement` records (or plain values) in the order they are inserted; the script resolves the subject,
metric and source ids and writes the rows. The snapshot date comes in as an argument.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

MEASUREMENTS_SNAPSHOT_FILE = "measurements_snapshot.json"

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


def metric_label(key):
    """(label, good_direction) of a metric key: the curated entry, else an auto-generated label."""
    return METRIC_META.get(key, (humanize(key), None))


def validate_snapshot_date(data, path):
    """The `synced_at` of the parsed snapshot file `data`, validated; RuntimeError naming `path` otherwise.
    The snapshot date lives in the private data (written once by backfill_dates.py), never in code."""
    value = data.get("synced_at") if isinstance(data, dict) else None
    try:
        if not isinstance(value, str):
            raise ValueError
        datetime.fromisoformat(value)
    except ValueError:
        raise RuntimeError(f"{path}: `synced_at` must be an ISO date or timestamp string, got {value!r}") from None
    return value


def parse_snapshot_text(text, path):
    """The snapshot date from the file's text (JSON); RuntimeError naming `path` if it is not valid."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{path} is not valid JSON ({e})") from e
    return validate_snapshot_date(data, path)


def missing_snapshot_message(path):
    return (f"{path} is missing: the measurements snapshot date is not stored in the data yet. "
            f"Run `python3 -m ingest.backfill_dates --apply` (see the README, 'Dates'); a date is never invented.")


@dataclass
class Measurement:
    subject: str
    metric: str
    value: object
    unit: str
    measured_at: str
    source: str            # a source key: a manual citekey, or a key of vault_db_sources()
    trust_level: str
    trust_rationale: str
    notes: Optional[str] = None
    recheck_by: Optional[str] = None
    recheck_rationale: Optional[str] = None


# The manual sources step 01 seeds, by the name this module uses for them.
MANUAL_SOURCES = {
    "dexa-2026": "bodyspec-dexa-2026-06-17", "dexa-2025": "bodyspec-dexa-2025-11-15", "labcorp": "labcorp-2025-01-24",
    "tape": "manual-tape-measurements", "strength": "strength-checkpoints-script", "unlocated": "unlocated-crp-esr-notes",
}


def vault_db_sources(vault, stale_note):
    """{key: (citekey, name, description, origin_path)} for the sources this step creates from vault tables,
    in two groups (the summary tables, then the raw logs), in creation order."""
    def src(citekey, table, label, desc):
        return (citekey, f"bodybuilding.db table {table} ({label})", desc + " " + stale_note, f"{vault}/bodybuilding.db#{table}")
    summary = {
        "nutrition": src("vault-db-nutrition-daily", "nutrition_daily", "daily logged nutrition + weight",
                         "Compiled daily rollup from MacroFactor exports, loaded into the vault's own bodybuilding.db."),
        "scale": src("vault-db-scale-readings", "scale_readings", "manual/compiled scale weight log",
                     "Weight readings compiled from various exports and screenshots."),
        "renpho": src("vault-db-renpho-scale", "renpho_scale_readings", "RENPHO smart-scale export",
                      "RENPHO bioimpedance scale readings. The vault's own note found body-fat%/FFM biased 14-32 points off DEXA on the same day -- weight itself is accurate to ~0.2 lb."),
        "volume": src("vault-db-muscle-volume-weekly", "muscle_volume_weekly", "computed training volume rollup",
                      "Per-muscle weekly set counts computed by the vault's own scripts."),
    }
    raw = {
        "sets": src("vault-db-workout-sets", "workout_sets", "per-set training log, 2024-12-12+",
                    "Per-set training log merged from Liftosaur and MacroFactor."),
        "jefit": src("vault-db-jefit-sets", "jefit_exercise_sets", "per-set training log, 2019-2024",
                     "Per-set training log exported from Jefit."),
        "mfp_m": src("vault-db-mfp-measurements", "mfp_measurements", "Fitbit-linked weight/sleep/steps, 2018-2026",
                     "Weight, body fat %, steps, sleep from Fitbit via MyFitnessPal."),
        "mfp_e": src("vault-db-mfp-exercise-log", "mfp_exercise_log", "cardio/activity log",
                     "Cardio/activity logged via MyFitnessPal."),
        "mfp_n": src("vault-db-mfp-nutrition-log", "mfp_nutrition_log", "per-meal macro/micro totals",
                     "Per-meal nutrition totals via MyFitnessPal, 2018-2026."),
        "food": src("vault-db-nutrition-food-log", "nutrition_food_log", "individual logged food items",
                    "Individual food-item entries from MacroFactor."),
        "micro": src("vault-db-micronutrients", "micronutrients", "daily micronutrient totals",
                     "Daily micronutrient totals compiled from MacroFactor exports."),
    }
    return summary, raw


def stale_note(snapshot_date):
    return (f"Synced into knowledge.db as of {snapshot_date} -- a snapshot of a live, "
            f"actively-updated vault table, not a re-syncing link.")


# ---------------------------------------------------------------- DEXA

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
DEXA_SCANS = [("2025-11-15", "dexa-2025"), ("2026-06-17", "dexa-2026")]


def dexa_measurements(scan, scan_date, source):
    """The readings of one DEXA scan row (a mapping), in insertion order."""
    out = []
    for metric, unit, col, subject, note in DEXA_ROWS:
        val = scan[col]
        if val is None:
            continue
        out.append(Measurement(subject, metric, val, unit, scan_date, source, "verified",
                               "Direct clinical DEXA measurement (BodySpec), read from the vault's own structured bodycomp_dexa table.",
                               note))
    ffm = (scan["lean_tissue_lbs"] or 0) + (scan["bmc_lbs"] or 0)
    out.append(Measurement("body-composition", "fat_free_mass_lb", ffm, "lb", scan_date, source, "verified",
                           "Direct clinical DEXA measurement (BodySpec).",
                           "= lean_tissue_lb + bone_mineral_content_lb, per the vault's own v_dexa view."))
    appendicular = (scan["arms_lean_lbs"] or 0) + (scan["legs_lean_lbs"] or 0)
    out.append(Measurement("body-composition", "appendicular_lean_lb", appendicular, "lb", scan_date, source, "verified",
                           "Direct clinical DEXA measurement (BodySpec).", "= arms_lean_lb + legs_lean_lb."))
    height_m = (scan["height_in"] or 0) * 0.0254
    if height_m:
        lmi = (scan["lean_tissue_lbs"] or 0) * 0.45359237 / (height_m ** 2)
        almi = appendicular * 0.45359237 / (height_m ** 2)
        out.append(Measurement("body-composition", "lmi", round(lmi, 2), "kg/m²", scan_date, source, "verified",
                               "Lean mass index, derived from the DEXA scan (lean soft tissue / height², bone excluded)."))
        out.append(Measurement("body-composition", "almi", round(almi, 2), "kg/m²", scan_date, source, "verified",
                               "Appendicular lean mass index, derived from the DEXA scan."))
    return out


# ---------------------------------------------------------------- bloodwork

BLOOD_METRIC_MAP = {"LH": "lh_miu_ml", "FSH": "fsh_miu_ml", "Testosterone total": "testosterone_total_ng_dl",
                    "Testosterone free (direct)": "testosterone_free_pg_ml", "Estradiol": "estradiol_pg_ml",
                    "CRP": "crp_mg_l", "Sed rate (ESR)": "esr_mm_hr"}
FSH_RECHECK_RATIONALE = ("Panel is now over 19 months old and predates a documented 13 lb weight loss; the "
                         "vault treats repeating this panel as its top-priority action item, since exogenous "
                         "testosterone use before a redraw would permanently confound future interpretation "
                         "of the unexplained FSH result.")


def bloodwork_measurement(row):
    """The reading of one bloodwork row, or None for an analyte this step does not ingest. A value whose
    source document the vault could not locate is carried forward as unverified."""
    metric = BLOOD_METRIC_MAP.get(row["analyte"])
    if not metric:
        return None
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
    recheck_rationale = FSH_RECHECK_RATIONALE if metric == "fsh_miu_ml" else None
    if unlocated:
        return Measurement("hormones-endocrinology", metric, row["value"], row["unit"], row["date"], "unlocated", "unverified",
                           "Value is carried forward from prior vault notes; the vault's own audit could not locate the underlying lab report for it.",
                           " ".join(note_parts))
    return Measurement("hormones-endocrinology", metric, row["value"], row["unit"], row["date"], "labcorp", "verified",
                       "Direct primary lab result (Labcorp), read from the vault's own structured bloodwork table.",
                       " ".join(note_parts), recheck_by, recheck_rationale)


# ---------------------------------------------------------------- tape, strength

TAPE_COLUMNS = ("neck_in", "shoulders_in", "chest_in", "upper_arm_in", "forearm_in", "waist_in",
                "hips_in", "thigh_in", "calf_in", "wrist_in", "ankle_in")


def tape_measurements(row):
    out = []
    for col in TAPE_COLUMNS:
        val = row[col]
        if val is None:
            continue
        out.append(Measurement("body-composition", col, val, "in", row["date"], "tape", "high",
                               "Self-measured with a tape measure -- prone to placement/tension variance between sessions, but a direct measurement."))
    return out


STRENGTH_LIFTS = [("squat", "squat_bw_ratio", "squat_as_of"), ("bench", "bench_bw_ratio", "bench_as_of"),
                  ("deadlift", "deadlift_bw_ratio", "deadlift_as_of")]


def strength_measurements(latest):
    """The e1RM and bodyweight-ratio readings of the latest strength checkpoint row."""
    out = []
    for lift, ratio_col, as_of_col in STRENGTH_LIFTS:
        e1rm = latest[f"{lift}_lbs"]
        as_of = latest[as_of_col]
        if e1rm is None:
            continue
        stale = "STALE" in (latest["note"] or "") and lift in (latest["note"] or "")
        recheck_by = "as soon as the next working set of this lift is logged" if stale else None
        recheck_rationale = ("The vault's own scripts/strength_checkpoint.py flags this specific figure "
                             f"stale as of the {latest['date']} snapshot.") if stale else None
        out.append(Measurement("strength-progression-norms", f"{lift}_e1rm_lb", e1rm, "lb", as_of, "strength", "high",
                               "Auto-computed estimated 1RM from logged training sets, not a tested single-rep max.",
                               f"checkpoint as of {latest['date']} snapshot", recheck_by, recheck_rationale))
        out.append(Measurement("strength-progression-norms", ratio_col, latest[ratio_col], "ratio", as_of, "strength", "high",
                               "Bodyweight ratio derived from the e1RM checkpoint and same-day bodyweight.",
                               f"checkpoint as of {latest['date']} snapshot"))
    return out


# ---------------------------------------------------------------- daily / weekly summaries

NUTRITION_METRICS = [
    ("weight_lbs", "weight_lb", "lb", "body-composition"), ("trend_weight_lbs", "trend_weight_lb", "lb", "body-composition"),
    ("calories_kcal", "logged_calories_kcal", "kcal", "nutrition-energy-balance"), ("protein_g", "logged_protein_g", "g", "nutrition-energy-balance"),
    ("fat_g", "logged_fat_g", "g", "nutrition-energy-balance"), ("carbs_g", "logged_carbs_g", "g", "nutrition-energy-balance"),
    ("expenditure_kcal", "modeled_expenditure_kcal", "kcal", "nutrition-energy-balance"),
]


def nutrition_measurements(row):
    return [Measurement(subject, metric, row[col], unit, row["date"], "nutrition", "verified",
                        "Self-logged via MacroFactor, compiled into the vault's own structured daily table.")
            for col, metric, unit, subject in NUTRITION_METRICS if row[col] is not None]


def scale_measurements(row):
    out = []
    if row["weight_lbs"] is not None:
        out.append(Measurement("body-composition", "scale_weight_lb", row["weight_lbs"], "lb", row["date"], "scale", "high",
                               "Manual/compiled scale reading."))
    if row["body_fat_pct"] is not None:
        out.append(Measurement("body-composition", "scale_bodyfat_pct", row["body_fat_pct"], "%", row["date"], "scale", "low",
                               "Bioimpedance-scale body-fat estimate, not DEXA.", "Not DEXA."))
    return out


def renpho_measurements(row):
    out = []
    if row["weight_lbs"] is not None:
        out.append(Measurement("body-composition", "scale_weight_lb", row["weight_lbs"], "lb", row["date"], "renpho", "high",
                               "RENPHO scale weight -- accurate to ~0.2 lb even though its body-composition estimates are not."))
    if row["body_fat_pct"] is not None:
        out.append(Measurement("body-composition", "scale_bodyfat_pct", row["body_fat_pct"], "%", row["date"], "renpho", "low",
                               "RENPHO bioimpedance estimate, found 14.3 points high vs. DEXA on a same-day comparison.", "Not DEXA."))
    if row["fat_free_mass_lbs"] is not None:
        out.append(Measurement("body-composition", "scale_fat_free_mass_lb", row["fat_free_mass_lbs"], "lb", row["date"], "renpho", "low",
                               "RENPHO bioimpedance estimate, found 22.4 lb high vs. DEXA on a same-day comparison.", "Not DEXA."))
    return out


def muscle_name(col):
    """The muscle a `<muscle>_sets` column of muscle_volume_weekly counts."""
    return col.replace("_sets", "").replace("_", " ")


# ---------------------------------------------------------------- raw logs

MFP_M_COLS = [("weight_lbs", "scale_weight_lb", "lb"), ("fitbit_body_fat_pct", "fitbit_bodyfat_pct", "%"),
              ("fitbit_steps", "fitbit_steps", "steps"), ("fitbit_sleep_minutes", "fitbit_sleep_minutes", "min")]


def mfp_measurements(row):
    out = []
    for col, metric, unit in MFP_M_COLS:
        if row[col] is None:
            continue
        trust = "unverified" if metric == "fitbit_bodyfat_pct" else "high"
        rationale = ("Wrist-wearable body-fat estimate -- generally unreliable, not corroborated against DEXA."
                     if metric == "fitbit_bodyfat_pct" else "Fitbit-synced measurement via MyFitnessPal.")
        out.append(Measurement("body-composition", metric, row[col], unit, row["date"], "mfp_m", trust, rationale))
    return out


def exercise_log_measurements(row):
    note = f"Activity: {row['exercise']} ({row['type']})" if row["exercise"] else None
    out = []
    if row["exercise_calories"] is not None:
        out.append(Measurement("nutrition-energy-balance", "cardio_calories_kcal", row["exercise_calories"], "kcal", row["date"],
                               "mfp_e", "high", "Self-logged/app-estimated exercise calories via MyFitnessPal.", note))
    if row["exercise_minutes"] is not None:
        out.append(Measurement("nutrition-energy-balance", "cardio_minutes", row["exercise_minutes"], "min", row["date"],
                               "mfp_e", "high", "Self-logged exercise duration via MyFitnessPal.", note))
    return out


def micronutrient_unit(col):
    """The unit a `<nutrient>_<unit>` column name carries ('unit' if it has none)."""
    m = re.match(r"^(.*)_([a-z]+)$", col)
    return m.group(2) if m else "unit"


def micronutrient_measurements(row, cols):
    return [Measurement("nutrition-energy-balance", col, row[col], micronutrient_unit(col), row["date"], "micro", "verified",
                        "Self-logged, compiled from MacroFactor daily micronutrient export.")
            for col in cols if row[col] is not None]
