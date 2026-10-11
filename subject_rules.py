"""Subjects as values: the vocabulary, the validation of a subjects file, canonical-name resolution and the pure
edits.

Model ring: imports nothing else in the repo and does no I/O. `subjects_store` keeps reading the file, the atomic
write and the git flow; what a subject entry may say (names and aliases claimed once, a parent chain that never
loops, a replacement that is a real subject) and how an edit changes a list of them lives here.
"""
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


@dataclass(frozen=True)
class Subject:
    """One entry of the subject hierarchy."""
    name: str
    domain: Optional[str] = None
    parent: Optional[str] = None
    relation: Optional[str] = None
    description: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    deprecated: bool = False
    replaced_by: Optional[str] = None

    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], d["domain"], d["parent"], d["relation"], d["description"], tuple(d["aliases"]),
                   bool(d["deprecated"]), d["replaced_by"])

    def to_dict(self):
        return {"name": self.name, "domain": self.domain, "parent": self.parent, "relation": self.relation,
                "description": self.description, "aliases": list(self.aliases), "deprecated": self.deprecated,
                "replaced_by": self.replaced_by}


VERSION = 1


RELATIONS = ("broader", "part-of", "subtype-of", "member-of")  # keep in sync with the CHECK in schema.sql


DEFAULT_RELATION = "broader"


DEFAULT_DOMAIN = "health-and-fitness"  # the schema default


SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


_ENTRY_KEYS = {"name", "domain", "parent", "relation", "description", "aliases", "deprecated", "replaced_by"}


class SubjectsError(Exception):
    """subjects.json is unreadable or breaks a rule. The message is safe to show as is."""


def _entry(name, domain=None, parent=None, relation=None, description=None, aliases=(), deprecated=False, replaced_by=None):
    return Subject(name, domain, parent, relation, description, tuple(aliases), bool(deprecated), replaced_by).to_dict()


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


def _index(entries):
    by_name = {e["name"]: e for e in entries}
    by_alias = {a: e for e in entries for a in e["aliases"]}
    return by_name, by_alias


def identify(name, find_name: Callable, find_alias: Callable):
    """(key, via): the one definition of which subject a name means, wherever the subjects live. `find_name(name)`
    and `find_alias(name)` return the subject's key (a canonical name, a row id) or None. A subject's own name wins
    over an alias; `via` is 'name' | 'alias' | 'unknown' and the key is None for an unknown name."""
    key = find_name(name)
    if key is not None:
        return key, "name"
    key = find_alias(name)
    if key is not None:
        return key, "alias"
    return None, "unknown"


def canonical(name, entries):
    """(canonical_name, via) with via 'name' | 'alias' | 'unknown'. An unknown name is returned unchanged
    (a brand-new subject is allowed; the privacy 'unknown subject' rule decides what that means)."""
    by_name, by_alias = _index(entries or [])
    key, via = identify(name, lambda n: n if n in by_name else None,
                        lambda a: by_alias[a]["name"] if a in by_alias else None)
    return (name if key is None else key), via


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


# ------------------------------------------------------------------ the hierarchy decisions (what the build step loads)


class AliasRefused(ValueError):
    """An alias claim the store (or `refuse_alias`) will not take. `reason` is the short cause."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SubjectPlan:
    """What one subjects.json entry says to store: where the subject sits, what it means, and the claims on it.
    `domain` is already defaulted (used only when the subject does not exist yet); `relation` is the schema value
    (the default when there is no parent)."""
    name: str
    domain: str
    parent: Optional[str]
    relation: str
    description: Optional[str]
    deprecated: bool
    replaced_by: Optional[str]
    aliases: Tuple[str, ...]


def plan_hierarchy(entries) -> Tuple[SubjectPlan, ...]:
    """One SubjectPlan per entry, in file order. A pure decision: the defaults for domain and relation, nothing about
    which subjects already exist (a stored subject keeps its domain; the store decides that)."""
    return tuple(
        SubjectPlan(e["name"], e["domain"] or DEFAULT_DOMAIN, e["parent"],
                    (e["relation"] or DEFAULT_RELATION) if e["parent"] else DEFAULT_RELATION,
                    e["description"], bool(e["deprecated"]), e["replaced_by"], tuple(e["aliases"]))
        for e in entries)


def refuse_alias(alias, subject_names, claimed: Dict[str, str]) -> Optional[str]:
    """The reason `alias` cannot be claimed, or None. `subject_names` holds every stored subject name; `claimed`
    maps the aliases already stored to their subject. (The database triggers say the same; this is the decision.)"""
    if alias in subject_names:
        return "alias collides with a subject name"
    if alias in claimed:
        return f"alias already belongs to {claimed[alias]!r}"
    return None


@dataclass(frozen=True)
class HierarchyTally:
    """What the stored hierarchy holds now (the whole table, not only what one load added)."""
    with_parent: int
    aliases: int
    deprecated: int
