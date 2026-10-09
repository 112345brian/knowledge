"""Append one ad hoc fact: the use case. It applies the rules in `new_fact` (domain) through the ports in
`ports` (the facts file, the privacy rules, the clock, fresh ids, the default paths) and imports no
adapter. `add_fact` is the facade that binds it to the real adapters, keeps the public API and is also the
`python3 add_fact.py` script.
"""
import os

import new_fact
import privacy
from new_fact import AddResult, DataFileError


def validate_fact(ports, fact, db_path=None):
    """Returns (errors, notes). Never prints. Same rules and messages the CLI has always had."""
    db_path = ports.defaults.db_path if db_path is None else db_path
    errors, notes = new_fact.check_fact(fact), []
    if fact.source_citekey:
        error, note = ports.facts_file.citekey_problem(db_path, fact.source_citekey)
        if error:
            errors.append(error)
        if note:
            notes.append(note)
    return errors, notes


def build_entry(ports, fact, visibility=None):
    """The JSON entry seed_general_facts.py expects (see new_fact.build_entry). `visibility` is the
    resolved value from privacy.resolve_visibility (append_fact passes it); left None it falls back
    to the caller's request."""
    origin = fact.origin_path.strip() if fact.origin_path and fact.origin_path.strip() else None
    return new_fact.build_entry(fact, visibility, ports.ids.new_source_key(), ports.clock.now_iso(),
                                origin_sha256=ports.facts_file.file_sha256(origin) if origin else None)


def resolve_privacy(ports, fact, data_path, db_path):
    """The privacy rules (privacy_rules.json next to the data file) applied to one fact. Subject
    context comes from the db when it has a subjects table (parents for tag inheritance, and the set
    of known subjects) plus subjects already in the facts file; with no usable db the unknown-subject
    rule is not enforced (nothing to compare against). Raises privacy.PrivacyRulesError."""
    rules = ports.rules.load_rules(os.path.join(ports.facts_file.data_dir_of(data_path), privacy.RULES_FILENAME))
    parents, known = ports.facts_file.subject_tree(db_path)
    if known is not None:
        known |= ports.facts_file.file_subjects(data_path)
    return privacy.resolve_visibility(fact.subject, fact.statement, fact.visibility,
                                      rules.with_context(parents=parents, known_subjects=known),
                                      extra_text=(fact.notes, fact.trust_rationale, fact.recheck_rationale,
                                                  fact.source_quote, fact.source_locator, fact.applies_to))


def canonicalize_subject(ports, fact, data_path):
    """#43: file the fact under its canonical subject. An alias is rewritten in place (`fact.subject`) and
    reported in the returned notes; a deprecated subject is refused with the replacement named. With no
    subjects.json beside the data file nothing changes. Returns (notes, error_or_None); never raises."""
    canon, notes, error = ports.facts_file.canonical_subject(data_path, fact.subject)
    if error is None:
        fact.subject = canon
    return notes, error


def append_fact(ports, fact, data_path=None, db_path=None):
    """Validate and append one fact. Returns an AddResult; never prints or exits. The stored
    visibility is the most restrictive of the request, the subject tag and the keyword list (#31)."""
    data_path = ports.defaults.data_path if data_path is None else data_path
    db_path = ports.defaults.db_path if db_path is None else db_path
    errors, notes = validate_fact(ports, fact, db_path)
    if errors:
        return AddResult(ok=False, errors=errors, notes=notes)
    subject_notes, subject_error = canonicalize_subject(ports, fact, data_path)
    notes = notes + subject_notes
    if subject_error:
        return AddResult(ok=False, errors=[subject_error], notes=notes)
    try:
        resolution = resolve_privacy(ports, fact, data_path, db_path)
    except (privacy.PrivacyRulesError, DataFileError) as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    entry = build_entry(ports, fact, visibility=resolution.visibility)
    try:
        total = ports.facts_file.append_record(data_path, entry)
    except DataFileError as e:
        return AddResult(ok=False, errors=[str(e)], notes=notes)
    except OSError as e:
        return AddResult(ok=False, errors=[f"could not write {data_path}: {e}"], notes=notes)
    return AddResult(ok=True, notes=notes, entry=entry, total=total, privacy=resolution)
