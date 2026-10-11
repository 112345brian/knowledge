"""Apply the revision log to a built database, then re-assert the privacy floor (#30, #31).

The sequence is the policy and is fixed: (1) validate the append-only log against the facts loaded so far and
insert revisions >= 2, writing each touched fact's latest revision into its row (`revision_apply_rules` says what
and how); (2) apply the CURRENT privacy rules, raise-only, because a revision can reword a statement to contain
a listed name or write back a visibility the loading steps had raised; (3) raise every snapshot of a private
fact to private, because history is never more visible than the fact itself. A violation in (1) stops the run
before any rule is applied. Everything is read and written through the ports; nothing here knows SQL or files.
"""
from dataclasses import dataclass
from typing import List

import fact_state
import revision_apply_rules
from fact_privacy_table_port import FactPrivacyTable, PrivacyRulesReader
from fact_revision_table_port import FactRevisionTable, RevisionLogReader
from privacy_floor_service import PrivacyFloorDeps, apply_privacy_floor


@dataclass(frozen=True)
class RevisionLogDeps:
    """What applying the log alone needs."""
    log: RevisionLogReader
    revision_table: FactRevisionTable


@dataclass(frozen=True)
class RevisionApplyDeps:
    """What the whole pass needs: the log, the revision rows, the privacy rows and the rules."""
    log: RevisionLogReader
    revision_table: FactRevisionTable
    privacy_table: FactPrivacyTable
    rules_reader: PrivacyRulesReader


@dataclass(frozen=True)
class RevisionRun:
    applied: int
    raised: List[tuple]
    total_revisions: int


def apply_revision_log(deps, path) -> int:
    """Validate the log at `path` against the facts and revision-1 rows, insert revisions >= 2, write each
    touched fact's latest revision into its row and commit. Raises RevisionError (naming path:line) on any
    violation, before anything is written. Returns the number of revisions applied."""
    table = deps.revision_table
    fact_ids = table.fact_ids_by_source_key()
    originals = table.original_revisions()
    _, _, base = revision_apply_rules.originals_view(originals)
    records = deps.log.read_log(path, base)
    revision_apply_rules.validate(path, records, fact_ids, originals)
    for _, rec in records:
        table.record_revision(fact_ids[rec["source_key"]], rec)
    for key, rec in revision_apply_rules.latest_by_key(records).items():
        table.write_current_state(fact_ids[key], revision_apply_rules.current_state(rec, fact_ids))
    table.commit()
    return len(records)


def apply_fact_revisions(deps, log_path, rules_path) -> RevisionRun:
    """The whole pass, in order: revisions applied, privacy rules applied, history of private facts raised."""
    applied = apply_revision_log(RevisionLogDeps(deps.log, deps.revision_table), log_path)
    rules = deps.rules_reader.load_rules(rules_path)
    floor = apply_privacy_floor(PrivacyFloorDeps(deps.privacy_table), rules)
    table = deps.revision_table
    table.raise_history_to_private(fact_state.facts_with_private_history(table.visibility_by_fact()))
    table.commit()
    return RevisionRun(applied, floor["raised"], table.revision_count())
