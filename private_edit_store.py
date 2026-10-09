"""The shared edit-and-commit flow for hand-curated JSON files in knowledge-private (#43, #42).

`privacy tag`, `subject ...` and `entity ...` all do the same thing: load a file, apply a pure edit,
refuse on a dirty private repo, write atomically, commit only the files touched, and report. This is
that flow once, as a library function that never prints or exits (the CLI modules print).

    edit_files(paths, compute, write, what, message, ...)
      paths     every file the change writes (committed together, nothing else)
      compute() -> (changed, state)   pure; raises one of `error_types` to refuse (message shown as is)
      write(state)                    writes `paths` atomically; called only when changed and not a dry run

An edit that changes nothing writes and commits nothing and needs no clean tree. The commit never
pushes. Git operations arrive through the injected `PrivateGit` protocol.
"""
import os
from private_git_port import PrivateGit

class EditResult:
    def __init__(self, ok, changed=False, what=None, message=None, commit=None, commit_error=None, errors=(), notes=(),
                 detached=False, dry_run=False, path=None):
        self.ok, self.changed, self.what, self.message = ok, changed, what, message
        self.commit, self.commit_error, self.errors, self.notes = commit, commit_error, list(errors), list(notes)
        self.detached, self.dry_run, self.path = detached, dry_run, path

    def to_json(self):
        return {"ok": self.ok, "changed": self.changed, "what": self.what, "dry_run": self.dry_run, "path": self.path,
                "commit": self.commit, "commit_error": self.commit_error, "errors": self.errors, "notes": self.notes}


def edit_files(paths, compute, write, what, message, git: PrivateGit, allow_dirty=False, dry_run=False,
               error_types=(Exception,)):
    shown = paths[0]
    try:
        changed, state = compute()
    except error_types as e:
        return EditResult(False, errors=[str(e)], path=shown)
    if not changed:
        return EditResult(True, changed=False, what=what, path=shown, notes=[f"no change: {shown} already has this"])
    directory = os.path.dirname(os.path.abspath(shown))
    probe = directory
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    notes = []
    try:
        repo = git.find_repo(probe)
        if repo is None:
            notes.append(f"{directory} is not inside a git repository; the change will not be committed.")
        elif not allow_dirty:
            git.ensure_clean_tree(repo)
    except git.PrivateGitError as e:
        return EditResult(False, errors=[str(e)], path=shown)
    if dry_run:
        return EditResult(True, changed=True, what=what, message=message if repo else None, dry_run=True, path=shown, notes=notes)
    try:
        os.makedirs(directory, exist_ok=True)
        write(state)
    except (*error_types, OSError) as e:
        return EditResult(False, errors=[f"could not write {shown}: {e}"], path=shown)
    if repo is None:
        return EditResult(True, changed=True, what=what, path=shown, notes=notes)
    try:
        commit = git.commit_private_change(list(paths), message, repo)
        detached = git.is_detached(repo)
    except git.PrivateGitError as e:
        return EditResult(True, changed=True, what=what, message=message, path=shown, notes=notes,
                          commit_error=f"the change IS written to {shown} but is NOT committed: {e}")
    return EditResult(True, changed=True, what=what, message=message, commit=commit, detached=detached, path=shown, notes=notes)
