"""Editing and reading the privacy rules file (#31): the facade (composition root) that binds the use case in
`rules_edit_service` to the real adapters (`privacy_store` for the rules file, `private_git` for the safety
net) and keeps the public API. `knowledge.py privacy ...` calls this and imports no adapter itself.

    edit_rules(edit, describe, allow_dirty, dry_run) -> RulesEditResult   (writes and commits the rules file)
    load_rules() / rules_path() / rules_file_exists(path)                (read-only)
"""
import private_git
import privacy_store
import rules_edit_service
from wiring import bind_all

API = ("edit_rules", "rules_path", "rules_file_exists", "load_rules")


def compose(locations):
    """The privacy-rules editing API wired to the real adapters for these `locations`."""
    deps = rules_edit_service.RulesEditDeps(rules=privacy_store, git=private_git, locations=locations)
    return bind_all(rules_edit_service, deps, API)
