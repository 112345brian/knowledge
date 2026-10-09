"""Editing and reading the privacy rules file (#31): the facade (composition root) that binds the use case in
`rules_edit_service` to the real adapters (`privacy_store` for the rules file, `private_git` for the safety
net) and keeps the public API. `knowledge.py privacy ...` calls this and imports no adapter itself.

    edit_rules(edit, describe, allow_dirty, dry_run) -> RulesEditResult   (writes and commits the rules file)
    load_rules() / rules_path() / rules_file_exists(path)                (read-only)
"""
import privacy  # noqa: F401  (rules_edit.privacy.PrivacyRulesError)
import private_git
import privacy_store
import rules_edit_service
from ports import Ports, bind
from privacy import PrivacyRulesError  # noqa: F401  (raised for an unusable rules file or an invalid edit)
from private_git import PrivateGitError  # noqa: F401  (raised for a dirty knowledge-private tree)
from rules_edit_service import RulesEditResult  # noqa: F401

PORTS = Ports(rules=privacy_store, git=private_git)

edit_rules = bind(rules_edit_service.edit_rules, PORTS)


def rules_path():
    """Where the privacy rules file is (PRIVATE_DATA_DIR/privacy_rules.json)."""
    return privacy_store.rules_path()


def rules_file_exists(path):
    return privacy_store.rules_file_exists(path)


def load_rules(path=None):
    """The rules in `path` (default: the rules file). Raises PrivacyRulesError if it is unusable."""
    return privacy_store.load_rules(rules_path() if path is None else path)
