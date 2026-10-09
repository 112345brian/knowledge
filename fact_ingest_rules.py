"""Fact ingest rules (steps 04 and 11), domain: no file, no db.

Which entries of the fact files are loadable, the is_personal heuristic for the vault-extracted facts, the
vault note an extracted fact came from, and the validated `date_added`. The scripts read the files and
write the rows; they call these.
"""
import re
from datetime import datetime

from fact_rules import VALID_TRUST, VALID_VISIBILITY
from revisions import SOURCE_KEY_RE, VALID_STATUS

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


def resolve_origin_path(notes_text, vault):
    """Extract the vault .md file a batch-extracted fact's `notes` references."""
    if not notes_text:
        return None
    m = re.search(r"(" + re.escape(vault) + r"/[^,]+?\.md)", notes_text)
    if m:
        return m.group(1)
    if notes_text.startswith("harm-reduction/"):
        m = re.match(r"^(harm-reduction/[^,]+?\.md)", notes_text)
        if m:
            return f"{vault}/{m.group(1)}"
    m = re.match(r'^([A-Za-z0-9][^,]*?\.md)', notes_text)
    if m:
        fname = m.group(1)
        if fname in HARM_REDUCTION_FILES:
            return f"{vault}/harm-reduction/{fname}"
        return f"{vault}/{fname}"
    # filenames containing a comma (e.g. "Volume Is the Variable, Not Frequency.md")
    # defeat the comma-terminated regex above -- fall back to a known list.
    for known in ["Volume Is the Variable, Not Frequency.md"]:
        if notes_text.startswith(known):
            return f"{vault}/{known}"
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


class Skip(Exception):
    """An entry that is not loaded: `warning` is printed by the script (None = a silent skip)."""
    def __init__(self, warning=None):
        super().__init__(warning)
        self.warning = warning


def screen_item(item):
    """(subject, statement, trust, visibility, status) of a loadable entry. Raises Skip for one that is not:
    a missing subject / statement, an unknown trust level (silent), an invalid visibility or status (with a
    warning). Unmarked visibility is private; it is never derived from is_personal."""
    subj = (item.get("subject") or "").strip()
    stmt = (item.get("statement") or "").strip()
    trust = (item.get("trust_level") or "").strip()
    if not subj or not stmt or trust not in VALID_TRUST:
        raise Skip()
    visibility = item.get("visibility")
    if visibility is None:
        visibility = "private"  # unmarked facts are private; never derived from is_personal
    if not isinstance(visibility, str) or visibility not in VALID_VISIBILITY:
        raise Skip(f"  WARNING -- skipping fact with invalid visibility {visibility!r}: {stmt[:60]!r}")
    status = item.get("status") or "active"
    if status not in VALID_STATUS:
        raise Skip(f"  WARNING -- skipping fact with invalid status {status!r}: {stmt[:60]!r}")
    return subj, stmt, trust, visibility, status


def check_source_key(key, stmt):
    """The entry's source_key if it is well formed; ValueError otherwise."""
    if not SOURCE_KEY_RE.match(key):
        raise ValueError(f"invalid source_key {key!r} on fact {stmt[:60]!r}")
    return key


def require_date_added(item, filename, index):
    """The entry's own `date_added`, validated. A missing, null, blank or non-ISO value is a
    build error naming the file and entry -- never a made-up date (#35). Fix the data, or run
    backfill_dates.py for the original entries."""
    value = item.get("date_added")
    try:
        if not isinstance(value, str):
            raise ValueError
        datetime.fromisoformat(value)
    except ValueError:
        what = "has no `date_added`" if "date_added" not in item else f"has an invalid `date_added` {value!r}"
        raise ValueError(
            f"{filename}[{index}] ({str(item.get('statement') or '')[:60]!r}) {what}. A date is never invented: "
            f"set it by hand, or for the original entries run `python3 backfill_dates.py --apply`.") from None
    return value
