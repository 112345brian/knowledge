"""Commit every mutation of the knowledge-private JSON files, so git is a reliable
backup and safety net for them (issue #10). The queryable fact history is the revision
log (#30); this is the layer underneath it.

Reusable by any command that mutates a JSON file in knowledge-private (add-fact now;
approve, supersede, retract, set-visibility later). The pattern:

    repo = find_repo(os.path.dirname(path))      # None if the data dir is not in a git repo
    if repo and not allow_dirty:
        ensure_clean_tree(repo)                  # BEFORE writing; raises PrivateGitError
    ... write the JSON file ...
    if repo:
        commit_private_change([path], "approve: <subject> (<trust_level>)", repo)

Never pushes. Library code never prints or exits; callers decide how to report a
PrivateGitError. Git commands run via argv lists (no shell) with a fixed English locale.
"""
import os
import subprocess


class PrivateGitError(Exception):
    """A git precondition or commit failed. The message is safe to show the user as is."""


def _git(repo_dir, *args):
    try:
        return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True,
                              env={**os.environ, "LC_ALL": "C"})
    except FileNotFoundError as e:
        raise PrivateGitError("git is not installed or not on PATH") from e


def _detail(proc):
    return (proc.stderr.strip() or proc.stdout.strip() or f"git exited {proc.returncode}")


def find_repo(directory):
    """Top-level dir of the git repo containing `directory`, or None if it is not in one
    (or does not exist). Works with any KNOWLEDGE_PRIVATE_DIR layout."""
    if not os.path.isdir(directory):
        return None
    proc = _git(directory, "rev-parse", "--show-toplevel")
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def ensure_clean_tree(repo_dir):
    """Raise PrivateGitError unless the working tree has no changes at all: staged,
    unstaged or untracked (ignored files don't count). A commit can then never bundle
    unrelated edits."""
    proc = _git(repo_dir, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        raise PrivateGitError(f"could not read git status in {repo_dir}: {_detail(proc)}")
    lines = proc.stdout.splitlines()
    if lines:
        shown = "\n".join("    " + l for l in lines[:10])
        more = f"\n    ... and {len(lines) - 10} more" if len(lines) > 10 else ""
        raise PrivateGitError(
            f"{repo_dir} has uncommitted changes; refusing so they are not mixed into this commit. "
            f"Commit or discard them first (or pass --allow-dirty for a deliberate batch edit):\n{shown}{more}")


def is_detached(repo_dir):
    """True when HEAD is detached: commits made there are easy to lose, so callers may warn."""
    return _git(repo_dir, "symbolic-ref", "-q", "HEAD").returncode != 0


def commit_private_change(paths, message, repo_dir):
    """`git add` + `git commit` exactly `paths` (and nothing else already staged) in
    `repo_dir`. Returns the short hash of the new commit; raises PrivateGitError (with
    git's own message: hook output, missing identity, nothing to commit, ignored file...)
    on failure. Does not push."""
    paths = [os.path.realpath(p) for p in paths]
    if not paths:
        raise PrivateGitError("no paths to commit")
    proc = _git(repo_dir, "add", "--", *paths)
    if proc.returncode != 0:
        raise PrivateGitError(f"git add failed: {_detail(proc)}")
    proc = _git(repo_dir, "commit", "-m", message, "--only", "--", *paths)
    if proc.returncode != 0:
        raise PrivateGitError(f"git commit failed: {_detail(proc)}")
    return _git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
