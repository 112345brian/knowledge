"""Apply the revision log to a built database, then re-assert the privacy floor (#30, #31).

The sequence is the policy and is fixed: (1) validate the append-only log against the facts loaded so far and
insert revisions >= 2, writing each touched fact's latest revision into its row (`revision_apply_rules` says what
and how); (2) apply the CURRENT privacy rules, raise-only, because a revision can reword a statement to contain
a listed name or write back a visibility the loading steps had raised; (3) raise every snapshot of a private
fact to private, because history is never more visible than the fact itself. A violation in (1) stops the run
before any rule is applied. Everything is read and written through the foundation's repositories (`build_repositories_port`); nothing here
knows SQL or files.
"""
from dataclasses import dataclass
from typing import List

import revision_apply_rules
from build_repositories_port import PrivacyRepository, RevisionRepository, UnitOfWork


@dataclass(frozen=True)
class RevisionApplyDeps:
    """What the whole pass needs: the revision history, the privacy rules and the unit of work."""
    revision_repository: RevisionRepository
    privacy_repository: PrivacyRepository
    unit_of_work: UnitOfWork


@dataclass(frozen=True)
class RevisionRun:
    applied: int
    raised: List[tuple]
    total_revisions: int


def apply_revision_log(revisions: RevisionRepository, unit_of_work: UnitOfWork, path: str) -> int:
    """Validate the log at `path` against the facts and revision-1 rows, insert revisions >= 2, write each
    touched fact's latest revision into its row and commit. Raises RevisionError (naming path:line) on any
    violation, before anything is written. Returns the number of revisions applied.

    Ids are allocated in call order: every record is inserted in log order, then the write-backs follow in order of
    each fact's first appearance."""
    fact_ids = revisions.fact_ids_by_source_key()
    firsts = revisions.first_revisions()
    records = revisions.read_log(path, revision_apply_rules.inherited_by_key(firsts))
    revision_apply_rules.validate(path, records, fact_ids, firsts)
    for _, rec in records:
        revisions.insert(fact_ids[str(rec["source_key"])], rec)
    for key, rec in revision_apply_rules.latest_by_key(records).items():
        revisions.write_back(revision_apply_rules.write_back(fact_ids[key], rec, fact_ids))
    unit_of_work.commit()
    return len(records)


def apply_fact_revisions(deps: RevisionApplyDeps, log_path: str, rules_path: str) -> RevisionRun:
    """The whole pass, in order: revisions applied (commit), privacy rules applied, history of private facts
    raised (commit). Two commits by contract (docs/build-acceptance.md): nothing reads the database between them,
    but the step's commit points are pinned, so they stay."""
    applied = apply_revision_log(deps.revision_repository, deps.unit_of_work, log_path)
    rules = deps.privacy_repository.load_rules(rules_path)
    outcome = deps.privacy_repository.apply_rules(rules)
    deps.privacy_repository.raise_history_of_private_facts()
    deps.unit_of_work.commit()
    return RevisionRun(applied, list(outcome.raised), deps.revision_repository.count())
