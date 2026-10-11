"""The fact ingest (the use case, through the port protocols; `fact_ingest` binds it to the adapters).

Pilot/batch fact files and general_facts.json are loaded into `facts`, `fact_sources` and `fact_revisions`
by one implementation of the decisions (`fact_ingest_decisions`): screen the entry (a Skip), check its source
key, refuse a duplicate, decide its row (personal flag, origin note, freshness, kind, validity, applies_to,
date_added, the implicit revision 1), write it through the fact repository, link the source it cites, and
after the last entry apply the privacy rules (raise-only). A step passes a profile and prints the report.

    report = ingest_fact_files(con)       # pilot_facts.json + facts_batch1-4.json
    report = ingest_general_facts(con)    # general_facts.json
    fact_ingest_decisions.render(report)  # the lines to print
"""
import json
from dataclasses import dataclass
from typing import List, Optional

import acquisition_rules
import fact_ingest_decisions as decisions
from fact_ingest_decisions import EntryRef, IngestedFact, IngestProfile, IngestReport
from fact_ingest_port import FactRepository, PrivacyFloor
from ingest import fact_ingest_rules
from inputs_port import Inputs
from locations import FACTS_FILENAME, Locations

HINTS_FILENAME = "fact_hints.json"


@dataclass(frozen=True)
class FactIngestDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    facts: FactRepository
    inputs: Inputs
    privacy_floor: PrivacyFloor
    locations: Locations


def read_hints(inputs, path):
    """The author's fact-ingest hints from the file at `path` (fact_hints.json in the private data dir; see
    fact_ingest_rules.Hints), or NO_HINTS when it is absent. A file that is not valid JSON or not a valid hints
    object fails the build."""
    try:
        data = inputs.read_json(path)
    except FileNotFoundError:
        return fact_ingest_rules.NO_HINTS
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e})") from e
    return fact_ingest_rules.parse_hints(data, path)


def load_fact_file_entries(deps) -> List[EntryRef]:
    """Every entry of pilot_facts.json and facts_batch1-4.json, in file order, each with its source key."""
    entries: List[EntryRef] = []
    for name in decisions.FACT_FILENAMES:
        batch = deps.inputs.read_json(deps.locations.in_data_dir(name))
        entries.extend(decisions.entry_refs(batch, name, is_pilot=name == decisions.PILOT_FILENAME))
    return entries


def load_general_entries(deps) -> List[EntryRef]:
    items = deps.inputs.read_json(deps.locations.in_data_dir(FACTS_FILENAME))
    return decisions.entry_refs(items, FACTS_FILENAME)


def ensure_vault_file(deps, con, path) -> Optional[int]:
    """The vault_files id for `path`, with its current fixity record (#38) written on first sight. A file that
    cannot be read gets file_state 'missing' and no hash; this never raises for a bad path. A file is hashed and
    its attributes read once per build, not once per fact."""
    if not path:
        return None
    found = deps.facts.find_vault_file(con, path)
    if found is not None and found[1]:
        return found[0]
    file_id = found[0] if found is not None else deps.facts.add_vault_file(con, path)
    fingerprint = deps.inputs.fingerprint(path)
    # #46: a vault file has no data record, so its custodial history comes from the macOS file attributes
    # alone (a no-op elsewhere); the acquisition resolver marks anything it fills in acquired_note.
    acquisition = deps.inputs.acquisition({k: None for k in acquisition_rules.COLUMNS}, path)
    deps.facts.record_vault_file(con, file_id, fingerprint, acquisition)
    return file_id


def ingest_entries(deps, con, entries, profile: IngestProfile, hints=fact_ingest_rules.NO_HINTS) -> IngestReport:
    """Load `entries` into the database `con` under `profile`; the report. A malformed entry, a duplicate key or a
    missing date raises ValueError (RevisionError for freshness, kind, validity, applies_to): the build fails."""
    # Fail early on a corrupt rules file; an absent one means empty rules (#31).
    rules = deps.privacy_floor.load_privacy_rules(deps.locations.privacy_rules())
    sources = deps.facts.source_ids_by_citekey(con)
    subjects = {}
    report = IngestReport(label=profile.label)

    for entry in entries:
        screened = decisions.screen(entry)
        if isinstance(screened, decisions.Skipped):
            report.skipped.append(screened)
            continue
        if deps.facts.fact_exists(con, screened.key):
            raise decisions.duplicate_key_error(screened)
        report.derived_keys += 0 if entry.has_own_key else 1

        plan = decisions.plan_fact(screened, profile, hints, deps.locations.notes_vault)
        if plan.subject not in subjects:
            subjects[plan.subject] = deps.facts.subject_id(con, plan.subject, plan.domain, profile.resolve_aliases)
        subject_id = subjects[plan.subject]
        origin_file_id = ensure_vault_file(deps, con, plan.origin_path)

        fact_id = deps.facts.insert_fact(con, decisions.with_ids(plan.row, subject_id, origin_file_id))
        deps.facts.insert_revision(con, fact_id, plan.revision)

        source_id = None
        if plan.citekey:
            source_id = sources.get(plan.citekey)
            if source_id is None:
                report.unknown_citekeys.add(plan.citekey)
            else:
                deps.facts.link_source(con, fact_id, source_id, plan.locator, plan.quote)
        report.ingested.append(IngestedFact(fact_id, screened.key, subject_id, source_id))

    # Re-apply the current privacy rules to every fact (raise-only; also tags subjects). Subjects' parent_id is
    # set by step 06, so the last fact step is the pass that sees the whole tree.
    applied = deps.privacy_floor.apply_privacy_rules(con, rules)
    deps.facts.commit(con)
    report.privacy_raised = len(applied["raised"])
    if profile.breakdowns:
        report.breakdowns = {column: deps.facts.facts_by(con, column) for column in decisions.BREAKDOWNS}
    return report


def ingest_fact_files(deps, con) -> IngestReport:
    """Load pilot_facts.json and facts_batch1-4.json (hand-authored, and extracted from the vault's notes)."""
    hints = read_hints(deps.inputs, deps.locations.in_data_dir(HINTS_FILENAME))   # the author's vault hints, or none
    return ingest_entries(deps, con, load_fact_file_entries(deps), decisions.FACT_FILES, hints)


def ingest_general_facts(deps, con) -> IngestReport:
    """Load general_facts.json: ad hoc facts with no separate source database behind them."""
    return ingest_entries(deps, con, load_general_entries(deps), decisions.GENERAL_FACTS)
