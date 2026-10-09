"""The subject hierarchy as data (#43). Library only; `knowledge.py subject ...` wraps the edits.

`subjects.json` in knowledge-private (next to privacy_rules.json) is the source of truth for how subjects
relate and what they mean; step 06 loads it into the `subjects` / `subject_aliases` tables. Shape:

    {"version": 1, "subjects": [
        {"name": "aas-legal", "domain": "health-and-fitness", "parent": "anabolic-steroids",
         "relation": "part-of", "description": "Law and enforcement around steroids",
         "aliases": ["steroid-law"], "deprecated": false, "replaced_by": null}, ...]}

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

import private_edit

SUBJECTS_FILENAME = "subjects.json"
VERSION = 1
RELATIONS = ("broader", "part-of", "subtype-of", "member-of")  # keep in sync with the CHECK in schema.sql
DEFAULT_RELATION = "broader"
DEFAULT_DOMAIN = "health-and-fitness"  # the schema default
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_ENTRY_KEYS = {"name", "domain", "parent", "relation", "description", "aliases", "deprecated", "replaced_by"}


class SubjectsError(Exception):
    """subjects.json is unreadable or breaks a rule. The message is safe to show as is."""


def data_path():
    from paths import PRIVATE_DATA_DIR  # lazy: this module imports without a private repo
    return os.path.join(PRIVATE_DATA_DIR, SUBJECTS_FILENAME)


def _entry(name, domain=None, parent=None, relation=None, description=None, aliases=(), deprecated=False, replaced_by=None):
    return {"name": name, "domain": domain, "parent": parent, "relation": relation, "description": description,
            "aliases": list(aliases), "deprecated": bool(deprecated), "replaced_by": replaced_by}


def parse(data, source="subjects.json"):
    """Validate decoded JSON; returns a list of normalized entry dicts (file order). Raises SubjectsError."""
    if not isinstance(data, dict) or not isinstance(data.get("subjects"), list):
        raise SubjectsError(f"{source}: expected an object with a 'subjects' list")
    if data.get("version", VERSION) != VERSION:
        raise SubjectsError(f"{source}: unsupported version {data.get('version')!r} (this code reads version {VERSION})")
    entries, seen = [], {}
    for i, raw in enumerate(data["subjects"]):
        where = f"{source}: subjects[{i}]"
        if not isinstance(raw, dict):
            raise SubjectsError(f"{where} is not an object")
        unknown = sorted(set(raw) - _ENTRY_KEYS)
        if unknown:
            raise SubjectsError(f"{where} has unknown key(s) {unknown}")
        name = raw.get("name")
        if not isinstance(name, str) or not SLUG_RE.match(name):
            raise SubjectsError(f"{where}: name {name!r} must be a lowercase kebab-case slug")
        where = f"{source}: subject {name!r}"
        if name in seen:
            raise SubjectsError(f"{where} is listed twice")
        domain = raw.get("domain")
        if domain is not None and not (isinstance(domain, str) and domain.strip()):
            raise SubjectsError(f"{where}: domain must be non-blank text")
        description = raw.get("description")
        if description is not None:
            if not isinstance(description, str):
                raise SubjectsError(f"{where}: description must be text")
            description = description.strip() or None
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) and SLUG_RE.match(a) for a in aliases):
            raise SubjectsError(f"{where}: aliases must be a list of lowercase kebab-case slugs")
        if len(set(aliases)) != len(aliases):
            raise SubjectsError(f"{where}: lists the same alias twice")
        deprecated = raw.get("deprecated", False)
        if not isinstance(deprecated, bool):
            raise SubjectsError(f"{where}: deprecated must be true or false")
        parent, relation, replaced_by = raw.get("parent"), raw.get("relation"), raw.get("replaced_by")
        if parent is not None and not (isinstance(parent, str) and SLUG_RE.match(parent)):
            raise SubjectsError(f"{where}: parent {parent!r} must be a subject name")
        if relation is not None and relation not in RELATIONS:
            raise SubjectsError(f"{where}: relation {relation!r} must be one of {list(RELATIONS)}")
        if relation is not None and parent is None:
            raise SubjectsError(f"{where}: relation {relation!r} given without a parent")
        if replaced_by is not None and not (isinstance(replaced_by, str) and SLUG_RE.match(replaced_by)):
            raise SubjectsError(f"{where}: replaced_by {replaced_by!r} must be a subject name")
        if replaced_by is not None and not deprecated:
            raise SubjectsError(f"{where}: replaced_by given but the subject is not deprecated")
        seen[name] = _entry(name, domain, parent, relation, description, aliases, deprecated, replaced_by)
        entries.append(seen[name])
    # cross-entry rules
    owner = {}  # every name and alias -> who claims it
    for e in entries:
        for label in (e["name"], *e["aliases"]):
            if label in owner:
                other = owner[label]
                raise SubjectsError(f"{source}: {label!r} is claimed twice ({other!r} and {e['name']!r}); "
                                    "a name or alias may belong to exactly one subject")
            owner[label] = e["name"]
    for e in entries:
        for field in ("parent", "replaced_by"):
            target = e[field]
            if target is None:
                continue
            if target == e["name"]:
                raise SubjectsError(f"{source}: subject {e['name']!r} has itself as {field}")
            if target not in seen:
                hint = " (it is an alias; use the subject's own name)" if target in owner else ""
                raise SubjectsError(f"{source}: subject {e['name']!r} has {field} {target!r}, which is not a subject in the file{hint}")
        for field in ("parent", "replaced_by"):
            chain, cur = [e["name"]], seen[e["name"]][field]
            while cur is not None:
                if cur in chain:
                    raise SubjectsError(f"{source}: {field} cycle: {' -> '.join(chain + [cur])}")
                chain.append(cur)
                cur = seen[cur][field]
    return entries


def read_file(path=None):
    """Entries from `path` (default: the private data dir's subjects.json); None when the file is absent
    (the caller decides what that means). Corrupt or invalid -> SubjectsError."""
    path = data_path() if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise SubjectsError(f"{path} is not valid JSON ({e}); left untouched") from e
    except (OSError, UnicodeDecodeError) as e:
        raise SubjectsError(f"could not read {path}: {e}") from e
    return parse(data, source=path)


def to_json(entries):
    """The on-disk form: sorted by name, defaults omitted, so diffs in knowledge-private are small."""
    out = []
    for e in sorted(entries, key=lambda e: e["name"]):
        item = {"name": e["name"]}
        if e["domain"]:
            item["domain"] = e["domain"]
        if e["parent"]:
            item["parent"] = e["parent"]
            if e["relation"]:
                item["relation"] = e["relation"]
        if e["description"]:
            item["description"] = e["description"]
        if e["aliases"]:
            item["aliases"] = sorted(e["aliases"])
        if e["deprecated"]:
            item["deprecated"] = True
            if e["replaced_by"]:
                item["replaced_by"] = e["replaced_by"]
        out.append(item)
    return {"version": VERSION, "subjects": out}


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
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ------------------------------------------------------------------ resolving names

def _index(entries):
    by_name = {e["name"]: e for e in entries}
    by_alias = {a: e for e in entries for a in e["aliases"]}
    return by_name, by_alias


def canonical(name, entries):
    """(canonical_name, via) with via 'name' | 'alias' | 'unknown'. An unknown name is returned unchanged
    (a brand-new subject is allowed; the privacy 'unknown subject' rule decides what that means)."""
    by_name, by_alias = _index(entries or [])
    if name in by_name:
        return name, "name"
    if name in by_alias:
        return by_alias[name]["name"], "alias"
    return name, "unknown"


def check_new_fact(name, entries):
    """(canonical_name, notes, error) for a new fact filed under `name`. `error` is None unless the
    canonical subject is deprecated, in which case it names the replacement."""
    canon, via = canonical(name, entries)
    notes = [f"subject {name!r} is an alias of {canon!r}; filed under {canon!r}"] if via == "alias" else []
    e = _index(entries or [])[0].get(canon)
    if e is not None and e["deprecated"]:
        use = f"use {e['replaced_by']!r} instead" if e["replaced_by"] else "no replacement is named; pick another subject"
        return canon, notes, f"subject {canon!r} is deprecated for new facts: {use}"
    return canon, notes, None


# ------------------------------------------------------------------ edits (pure; the caller save()s)
# Each returns (new_entries, changed) and raises SubjectsError for a refusal.

def _copy(entries):
    return [dict(e, aliases=list(e["aliases"])) for e in entries]


def ensure_entry(entries, name, domain=None):
    """Entries with `name` present (a minimal entry is added for a subject that exists in the db but not
    yet in the file). Raises SubjectsError for a name that is not a slug or is an alias of another subject."""
    if not isinstance(name, str) or not SLUG_RE.match(name):
        raise SubjectsError(f"{name!r} must be a lowercase kebab-case subject name")
    by_name, by_alias = _index(entries)
    if name in by_alias:
        raise SubjectsError(f"{name!r} is an alias of {by_alias[name]['name']!r}; use the subject's own name")
    if name in by_name:
        return _copy(entries)
    return _copy(entries) + [_entry(name, domain)]


def add_alias(entries, subject, alias, known=(), domain=None):
    """Add `alias` to `subject`. `known` = subject names that exist outside the file (the db), which may
    get a minimal entry; an unknown subject is refused."""
    if subject not in {e["name"] for e in entries} and subject not in set(known):
        raise SubjectsError(f"unknown subject {subject!r}")
    if not isinstance(alias, str) or not SLUG_RE.match(alias):
        raise SubjectsError(f"alias {alias!r} must be a lowercase kebab-case slug")
    new = ensure_entry(entries, subject, domain)
    by_name, by_alias = _index(new)
    if alias in by_alias:
        if by_alias[alias]["name"] == subject:
            return entries, False
        raise SubjectsError(f"alias {alias!r} already belongs to {by_alias[alias]['name']!r}")
    if alias in by_name or alias in set(known):
        raise SubjectsError(f"{alias!r} is already a subject name; an alias may not shadow a subject")
    by_name[subject]["aliases"].append(alias)
    return new, True


def describe(entries, subject, text, known=(), domain=None):
    """Set (or with blank text, clear) a subject's description."""
    if subject not in {e["name"] for e in entries} and subject not in set(known):
        raise SubjectsError(f"unknown subject {subject!r}")
    if not isinstance(text, str):
        raise SubjectsError("description must be text")
    text = text.strip() or None
    new = ensure_entry(entries, subject, domain)
    entry = {e["name"]: e for e in new}[subject]
    if entry["description"] == text:
        return entries, False
    entry["description"] = text
    return new, True


def deprecate(entries, subject, replaced_by=None, known=(), domain=None):
    """Mark `subject` deprecated for new facts, optionally naming its replacement. Existing facts keep their subject."""
    if subject not in {e["name"] for e in entries} and subject not in set(known):
        raise SubjectsError(f"unknown subject {subject!r}")
    new = ensure_entry(entries, subject, domain)
    if replaced_by is not None:
        if replaced_by == subject:
            raise SubjectsError("a subject cannot replace itself")
        if replaced_by not in {e["name"] for e in new}:
            if replaced_by not in set(known):
                raise SubjectsError(f"unknown replacement subject {replaced_by!r}")
            new = ensure_entry(new, replaced_by)
    entry = {e["name"]: e for e in new}[subject]
    if entry["deprecated"] and entry["replaced_by"] == replaced_by:
        return entries, False
    entry["deprecated"], entry["replaced_by"] = True, replaced_by
    parse(to_json(new), source="the edited subjects")  # surfaces a cycle before anything is written
    return new, True


# ------------------------------------------------------------------ the git flow (like the privacy rules)

EditResult = private_edit.EditResult


def edit_file(edit, what, message, allow_dirty=False, dry_run=False, path=None):
    """Load subjects.json (an absent file starts empty), apply `edit(entries) -> (new_entries, changed)`,
    refuse on a dirty private repo, write atomically and commit only that file (private_edit.edit_files).
    Never prints or exits. `what` / `message` describe the change for the report / commit."""
    path = data_path() if path is None else path

    def compute():
        new, changed = edit(read_file(path) or [])
        return changed, new

    return private_edit.edit_files([path], compute, lambda new: save(new, path), what, message,
                                   allow_dirty, dry_run, error_types=(SubjectsError,))
