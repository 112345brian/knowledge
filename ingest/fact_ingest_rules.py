"""Fact ingest rules (steps 04 and 11), domain: no file, no db.

Which entries of the fact files are loadable, the is_personal heuristic for the vault-extracted facts, the
vault note an extracted fact came from, and the validated `date_added`. The scripts read the files and
write the rows; they call these.
"""
import re
from dataclasses import dataclass
from datetime import datetime

from fact_rules import VALID_TRUST, VALID_VISIBILITY
from revisions import SOURCE_KEY_RE, VALID_STATUS

PRONOUN_RE = re.compile(r'\b(he|his|him|the vault owner|vault owner)\b', re.IGNORECASE)


@dataclass(frozen=True)
class Hints:
    """What the author's data teaches the ingest about their vault, kept in `fact_hints.json` in the private data
    dir (never in this repo): an absent file is `NO_HINTS`, and only the generic pronoun rule applies.

    fingerprints     regular expressions; a fact whose statement or notes match one is about the owner
    personal_notes   substrings of a fact's `notes` naming the vault notes whose original claims are about the owner
    note_folders     {folder: [file names]}: notes the extraction agents wrote without their subfolder
    whole_titles     note titles that contain a comma, which the comma-terminated path rule cannot read
    """
    fingerprints: tuple = ()
    personal_notes: tuple = ()
    note_folders: tuple = ()      # ((folder, (file names)), ...)
    whole_titles: tuple = ()

    @property
    def fingerprint_re(self):
        return re.compile("|".join(f"(?:{p})" for p in self.fingerprints), re.IGNORECASE) if self.fingerprints else None

    def folder_of(self, file_name):
        for folder, names in self.note_folders:
            if file_name in names:
                return folder
        return None


NO_HINTS = Hints()
HINT_KEYS = ("fingerprints", "personal_notes", "note_folders", "whole_titles")


def parse_hints(data, source="fact hints"):
    """`Hints` from the parsed JSON of fact_hints.json, or ValueError naming `source` and the problem."""
    if not isinstance(data, dict):
        raise ValueError(f"{source}: must be a JSON object with the keys {list(HINT_KEYS)}")
    unknown = [k for k in data if k not in HINT_KEYS and k != "version"]
    if unknown:
        raise ValueError(f"{source}: unknown key(s) {unknown}; known: {list(HINT_KEYS)}")

    def texts(key):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ValueError(f"{source}: `{key}` must be a list of non-blank strings")
        return tuple(value)

    fingerprints = texts("fingerprints")
    for pattern in fingerprints:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"{source}: fingerprint {pattern!r} is not a valid regular expression ({e})") from None
    folders = data.get("note_folders", {})
    if not isinstance(folders, dict) or not all(isinstance(k, str) and k.strip() and isinstance(v, list)
                                                and all(isinstance(n, str) and n.strip() for n in v) for k, v in folders.items()):
        raise ValueError(f"{source}: `note_folders` must map a folder name to a list of file names")
    return Hints(fingerprints=fingerprints, personal_notes=texts("personal_notes"),
                 note_folders=tuple((k, tuple(v)) for k, v in sorted(folders.items())), whole_titles=texts("whole_titles"))


def resolve_origin_path(notes_text, vault, hints=NO_HINTS):
    """Extract the vault .md file a batch-extracted fact's `notes` references."""
    if not notes_text:
        return None
    m = re.search(r"(" + re.escape(vault) + r"/[^,]+?\.md)", notes_text)
    if m:
        return m.group(1)
    m = re.match(r'^([A-Za-z0-9][^,]*?\.md)', notes_text)
    if m:
        fname = m.group(1)
        folder = hints.folder_of(fname)
        return f"{vault}/{folder}/{fname}" if folder else f"{vault}/{fname}"
    # a title with a comma defeats the comma-terminated pattern above: the hints list those titles
    for known in hints.whole_titles:
        if notes_text.startswith(known):
            return f"{vault}/{known}"
    return None


def classify_is_personal(statement, notes, is_original_claim, measured_link, hints=NO_HINTS):
    text = f"{statement or ''} {notes or ''}"
    if measured_link:
        return 1
    if PRONOUN_RE.search(text):
        return 1
    fingerprint = hints.fingerprint_re
    if fingerprint is not None and fingerprint.search(text):
        return 1
    if is_original_claim and any(f in (notes or "") for f in hints.personal_notes):
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
            f"set it by hand, or for the original entries run `python3 -m ingest.backfill_dates --apply`.") from None
    return value
