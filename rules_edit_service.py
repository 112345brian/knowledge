"""Edit the privacy rules file (#31): the use case behind `knowledge.py privacy tag|untag|add-keyword|
remove-keyword`, mirroring add-fact's git safety net (#10): load, apply the domain edit, refuse on a dirty
private repo, write, commit only the rules file. It works through the ports (`rules`, `git`) and imports
no adapter; the Typer command prints the result.
"""
from dataclasses import dataclass
from typing import Optional
from privacy_rules_port import Rules
from private_git_port import PrivateGit
from locations import Locations


@dataclass(frozen=True)
class RulesEditDeps:
    """Everything this service reaches the world through; the composition root supplies each one."""
    git: PrivateGit
    rules: Rules
    locations: Locations


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


def edit_rules(deps, edit, describe, allow_dirty, dry_run):
    """`edit(rules) -> (new_rules, changed)`; `describe(old, new) -> (what, commit_message)`. An edit that
    changes nothing writes and commits nothing (and needs no clean tree). Raises privacy.PrivacyRulesError
    for unusable rules or an invalid edit and git's PrivateGitError for a dirty tree."""
    path = deps.locations.privacy_rules()
    rules = deps.rules.load_rules(path)
    new, changed = edit(rules)
    if not changed:
        return RulesEditResult(path, changed=False)
    what, message = describe(rules, new)
    result = RulesEditResult(path, what=what, message=message, dry_run=dry_run, directory=deps.rules.data_dir_of(path))
    result.repo = deps.git.find_repo(deps.rules.existing_ancestor(result.directory))
    if result.repo is None:
        result.not_in_git = True
    elif not allow_dirty:
        deps.git.ensure_clean_tree(result.repo)
    if dry_run:
        return result
    deps.rules.make_dirs(result.directory)
    deps.rules.save_rules(new, path)
    if result.repo is not None:
        try:
            result.committed = deps.git.commit_private_change([path], message, result.repo)
            result.detached = deps.git.is_detached(result.repo)
        except deps.git.PrivateGitError as e:
            result.commit_error = str(e)
    return result


def rules_path(deps):
    """Where the privacy rules file is (<private data dir>/privacy_rules.json)."""
    return deps.locations.privacy_rules()


def rules_file_exists(deps, path):
    return deps.rules.rules_file_exists(path)


def load_rules(deps, path=None):
    """The rules in `path` (default: the rules file). Raises PrivacyRulesError if it is unusable."""
    return deps.rules.load_rules(deps.locations.privacy_rules() if path is None else path)
