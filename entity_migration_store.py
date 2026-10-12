"""Moving privacy keywords onto private entities (#42). Library only; `knowledge.py entity migrate-keywords` wraps it.

This edits TWO files, so it lives apart from entities.py (which privacy.py imports) and commits them together.
What the migration does, and the proof that nothing gets less private, is `entity_migration_rules.plan`; this
adapter reads the two files, writes them (entities first, so there is never a moment with neither) and owns the
git flow. Before anything is written, every old keyword is re-checked against the NEW state.
"""
import os

import entities_store
import entity_migration_rules
import private_edit_store
import privacy
from private_git_port import PrivateGit
import privacy_store


def migrate_keywords(allow_dirty=False, dry_run=False, keep_keywords=False, *, git: PrivateGit, data_dir):
    """Convert every keyword in the rules file to a private entity (type 'other'). `keep_keywords` leaves the
    keyword list in place (only adds entities). Returns a private_edit_store.EditResult; never prints or exits."""
    rules_file = os.path.join(data_dir, privacy.RULES_FILENAME)
    entities_file = entities_store.data_path(data_dir)

    def compute():
        migration = entity_migration_rules.plan(privacy_store.load_rules(rules_file),
                                                entities_store.read_file(entities_file) or [], keep_keywords)
        return migration.changed, migration

    def write(migration):
        entities_store.save(migration.entities, entities_file)       # entities first: never a moment with neither
        privacy_store.save_rules(migration.rules, rules_file)

    return private_edit_store.edit_files([entities_file, rules_file], compute, write,
                                         "migrate keywords to private entities",
                                         "entities: migrate privacy keywords to private entities",
                                         git, allow_dirty, dry_run,
                                         error_types=(entities_store.EntitiesError, privacy.PrivacyRulesError))
