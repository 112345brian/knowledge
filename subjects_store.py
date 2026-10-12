"""The subject hierarchy as data (#43). Library only; `knowledge.py subject ...` wraps the edits.

`subjects.json` in knowledge-private (next to privacy_rules.json) is the source of truth for how subjects
relate and what they mean; step 06 loads it into the `subjects` / `subject_aliases` tables. Shape:

    {"version": 1, "subjects": [
        {"name": "subtopic", "domain": "example", "parent": "topic",
         "relation": "part-of", "description": "A short scope note",
         "aliases": ["alternate-name"], "deprecated": false, "replaced_by": null}, ...]}

Only `name` is required. Rules (a violation raises SubjectsError naming the subject; nothing is repaired):
  * names and aliases are lowercase kebab-case slugs; every name and alias is unique across BOTH, so an
    alias can never be (or shadow) a subject;
  * `parent` names another entry; `relation` (broader | part-of | subtype-of | member-of, default broader)
    says what the parent edge means and requires a parent; no cycles, one parent only;
  * `deprecated` marks a subject new facts must not use; `replaced_by` (optional, requires deprecated)
    names the entry to use instead and may not loop back.

`parent` stays the SINGLE inheritance path for privacy tags (#31): a tag on a parent covers its children
whatever the relation type says. The relation is a label, not a second tree.

An alias given as a fact's subject is resolved to the canonical subject (`canonical`); a deprecated
subject is refused for new facts with the replacement named (`check_new_fact`). Aliases are slugs, not
free text, so a lookup is an exact match.
"""
import contextlib
import json
import os
import re
import stat
import uuid

import private_edit_store
import locks
from private_git_port import PrivateGit

from locations import SUBJECTS_FILENAME  # noqa: E402  (one definition of the file names, with Locations)
from subject_rules import (DEFAULT_DOMAIN, DEFAULT_RELATION, RELATIONS, SLUG_RE, VERSION, SubjectsError, Subject,  # noqa: F401
                           _ENTRY_KEYS, _copy, _entry, _index, add_alias, canonical, check_new_fact, deprecate, describe,
                           ensure_entry, parse, to_json)  # (defined with the rules)
from locations import join_path


def data_path(data_dir):
    return join_path(data_dir, SUBJECTS_FILENAME)


def read_file(path):
    """Entries from `path` (a subjects.json); None when the file is absent (the caller decides what that
    means). Corrupt or invalid -> SubjectsError."""
    try:
        with locks.open_retrying(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise SubjectsError(f"{path} is not valid JSON ({e}); left untouched") from e
    except (OSError, UnicodeDecodeError) as e:
        raise SubjectsError(f"could not read {path}: {e}") from e
    return parse(data, source=path)


def save(entries, path):
    """Atomic write (temp file + os.replace). Validates by parsing what it is about to write."""
    data = to_json(entries)
    parse(data, source="subjects to write")  # never write a file the loader would refuse
    tmp = f"{os.path.abspath(path)}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)  # the umask applies; no process-wide change
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        locks.replace_file(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------ resolving names


# ------------------------------------------------------------------ edits (pure; the caller save()s)
# Each returns (new_entries, changed) and raises SubjectsError for a refusal.


# ------------------------------------------------------------------ the git flow (like the privacy rules)

EditResult = private_edit_store.EditResult


def edit_file(edit, what, message, allow_dirty=False, dry_run=False, *, path, git: PrivateGit):
    """Load subjects.json (an absent file starts empty), apply `edit(entries) -> (new_entries, changed)`,
    refuse on a dirty private repo, write atomically and commit only that file (private_edit_store.edit_files).
    Never prints or exits. `what` / `message` describe the change for the report / commit."""

    def compute():
        new, changed = edit(read_file(path) or [])
        return changed, new

    return private_edit_store.edit_files([path], compute, lambda new: save(new, path), what, message, git,
                                   allow_dirty, dry_run, error_types=(SubjectsError,))
