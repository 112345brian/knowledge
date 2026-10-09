"""Edit the privacy rules file (#31): the use case behind `knowledge.py privacy tag|untag|add-keyword|
remove-keyword`, mirroring add-fact's git safety net (#10): load, apply the domain edit, refuse on a dirty
private repo, write, commit only the rules file. It works through the ports (`rules`, `git`) and imports
no adapter; the Typer command prints the result.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class RulesEditResult:
    path: str
    changed: bool = True
    what: str = ""
    message: str = ""
    dry_run: bool = False
    repo: Optional[str] = None
    committed: Optional[str] = None
    detached: bool = False
    not_in_git: bool = False
    directory: str = ""
    commit_error: Optional[str] = None


def edit_rules(ports, edit, describe, allow_dirty, dry_run):
    """`edit(rules) -> (new_rules, changed)`; `describe(old, new) -> (what, commit_message)`. An edit that
    changes nothing writes and commits nothing (and needs no clean tree). Raises privacy.PrivacyRulesError
    for unusable rules or an invalid edit and git's PrivateGitError for a dirty tree."""
    path = ports.rules.rules_path()
    rules = ports.rules.load_rules(path)
    new, changed = edit(rules)
    if not changed:
        return RulesEditResult(path, changed=False)
    what, message = describe(rules, new)
    result = RulesEditResult(path, what=what, message=message, dry_run=dry_run, directory=ports.rules.data_dir_of(path))
    result.repo = ports.git.find_repo(ports.rules.existing_ancestor(result.directory))
    if result.repo is None:
        result.not_in_git = True
    elif not allow_dirty:
        ports.git.ensure_clean_tree(result.repo)
    if dry_run:
        return result
    ports.rules.make_dirs(result.directory)
    ports.rules.save_rules(new, path)
    if result.repo is not None:
        try:
            result.committed = ports.git.commit_private_change([path], message, result.repo)
            result.detached = ports.git.is_detached(result.repo)
        except ports.git.PrivateGitError as e:
            result.commit_error = str(e)
    return result
