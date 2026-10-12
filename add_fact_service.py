"""Append one ad hoc fact: the use case. It applies the rules in `new_fact` (domain) through the ports in
`*_port` (the facts file, the privacy rules, the clock, fresh ids, the default paths) and imports no
adapter. `add_fact` is the facade that binds it to the real adapters, keeps the public API and is also the
`python3 add_fact.py` script.
"""
from dataclasses import dataclass
from typing import Optional

import locations
import new_fact
import privacy
from new_fact import AddResult, DataFileError
from clock_port import Clock
from facts_file_port import FactsFile
from ids_port import Ids
from privacy_rules_port import Rules
from private_git_port import PrivateGit
from locations import Locations


@dataclass(frozen=True)
class AddFactDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    facts_file: FactsFile
    rules: Rules
    clock: Clock
    ids: Ids
    locations: Locations
    git: PrivateGit


def validate_fact(deps, fact, db_path=None):
    """Returns (errors, notes). Never prints. Same rules and messages the CLI has always had."""
    db_path = deps.locations.db_path if db_path is None else db_path
    errors, notes = new_fact.check_fact(fact), []
    if fact.source_citekey:
        error, note = deps.facts_file.citekey_problem(db_path, fact.source_citekey)
        if error:
            errors.append(error)
        if note:
            notes.append(note)
    return errors, notes


def build_entry(deps, fact, visibility=None):
    """The JSON entry seed_general_facts.py expects (see new_fact.build_entry). `visibility` is the
    resolved value from privacy.resolve_visibility (append_fact passes it); left None it falls back
    to the caller's request."""
    origin = locations.canonical_path(fact.origin_path.strip()) if fact.origin_path and fact.origin_path.strip() else None
    return new_fact.build_entry(fact, visibility, deps.ids.new_source_key(), deps.clock.now_iso(),
                                origin_sha256=deps.facts_file.file_sha256(origin) if origin else None)


def resolve_privacy(deps, fact, data_path, db_path):
    """The privacy rules (privacy_rules.json next to the data file) applied to one fact. Subject
    context comes from the db when it has a subjects table (parents for tag inheritance, and the set
    of known subjects) plus subjects already in the facts file; with no usable db the unknown-subject
    rule is not enforced (nothing to compare against). Raises privacy.PrivacyRulesError."""
    rules = deps.rules.load_rules(deps.locations.privacy_rules(deps.facts_file.data_dir_of(data_path)))
    parents, known = deps.facts_file.subject_tree(db_path)
    if known is not None:
        known |= deps.facts_file.file_subjects(data_path)
    return privacy.resolve_visibility(fact.subject, fact.statement, fact.visibility,
                                      rules.with_context(parents=parents, known_subjects=known),
                                      extra_text=(fact.notes, fact.trust_rationale, fact.recheck_rationale,
                                                  fact.source_quote, fact.source_locator, fact.applies_to))


def canonicalize_subject(deps, fact, data_path):
    """#43: file the fact under its canonical subject. An alias is rewritten in place (`fact.subject`) and
    reported in the returned notes; a deprecated subject is refused with the replacement named. With no
    subjects.json beside the data file nothing changes. Returns (notes, error_or_None); never raises."""
    canon, notes, error = deps.facts_file.canonical_subject(data_path, fact.subject)
    if error is None:
        fact.subject = canon
    return notes, error


def append_fact(deps, fact, data_path=None, db_path=None):
    """Validate and append one fact. Returns an AddResult; never prints or exits. The stored
    visibility is the most restrictive of the request, the subject tag and the keyword list (#31)."""
    data_path = deps.locations.data_path if data_path is None else data_path
    db_path = deps.locations.db_path if db_path is None else db_path
    errors, notes = validate_fact(deps, fact, db_path)
    if errors:
        return AddResult(ok=False, errors=errors, notes=notes)
    subject_notes, subject_error = canonicalize_subject(deps, fact, data_path)
    notes = notes + subject_notes
    if subject_error:
        return AddResult(ok=False, errors=[subject_error], notes=notes)
    try:
        resolution = resolve_privacy(deps, fact, data_path, db_path)
    except (privacy.PrivacyRulesError, DataFileError) as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    entry = build_entry(deps, fact, visibility=resolution.visibility)
    try:
        total = deps.facts_file.append_record(data_path, entry)
    except DataFileError as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    except OSError as e:
        return AddResult(ok=False, errors=[f"could not write {data_path}: {e}"], notes=notes)
    return AddResult(ok=True, notes=notes, entry=entry, total=total, privacy=resolution)


@dataclass
class RecordResult:
    """`append_fact` plus the git safety net (#10), for the caller to report. Nothing is printed here.
    `refused` is a git refusal (a dirty tree) before anything was written; `commit_error` means the fact IS
    in the file but not committed; `repo` is None when the data file is not inside a git repository."""
    added: Optional[AddResult] = None
    refused: Optional[str] = None
    repo: Optional[str] = None
    data_dir: str = ""
    message: str = ""
    commit: Optional[str] = None
    detached: bool = False
    commit_error: Optional[str] = None

    @property
    def ok(self):
        return self.refused is None and self.added is not None and self.added.ok


def record_fact(deps, fact, data_path=None, db_path=None, allow_dirty=False):
    """Append one fact and, when the data file lives in a git repo, commit only that file. The tree must be
    clean first (unless allow_dirty). A non-git data dir (a throwaway test layout) is written without
    committing. Returns a RecordResult; never prints or exits."""
    data_path = deps.locations.data_path if data_path is None else data_path
    out = RecordResult(data_dir=deps.facts_file.data_dir_of(data_path))
    try:
        out.repo = deps.git.find_repo(out.data_dir)
        if out.repo is not None and not allow_dirty:
            deps.git.ensure_clean_tree(out.repo)
    except deps.git.PrivateGitError as e:
        out.refused = str(e)
        return out
    out.added = append_fact(deps, fact, data_path=data_path, db_path=db_path)
    if not out.added.ok or out.repo is None:
        return out
    out.message = f"add-fact: {fact.subject} ({fact.trust_level})"
    try:
        out.commit = deps.git.commit_private_change([data_path], out.message, out.repo)
        out.detached = deps.git.is_detached(out.repo)
    except deps.git.PrivateGitError as e:
        out.commit_error = str(e)
    return out
