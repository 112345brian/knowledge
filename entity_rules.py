"""Entities as values: the vocabulary, the validation of an entities file, matching and the pure edits.

Model ring: imports only `textmatch`, and does no I/O. `entities_store` (the file adapter) keeps the
reading, the atomic write and the git flow (the entity rows are `entity_repository_store`); everything about what an entity is, which names it
may claim and how an edit changes a list of them lives here, so the CLI and the privacy resolver share it without
reaching through an adapter.
"""
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Tuple

import textmatch

ENTITY_TYPES = ("person", "organization", "place", "project", "substance", "other")  # keep in sync with schema.sql


class EntitiesError(Exception):
    """entities.json is unreadable or breaks a rule. The message is safe to show as is."""

VERSION = 1
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_KEYS = {"id", "canonical_name", "type", "aliases", "private", "external_id", "notes"}


@dataclass(frozen=True)
class Entity:
    """One entity: a person, organization, place, project or substance facts can be about."""
    id: str
    canonical_name: str
    type: str
    aliases: Tuple[str, ...] = ()
    private: bool = False
    external_id: Optional[str] = None
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, d):
        return cls(d["id"], d["canonical_name"], d["type"], tuple(d["aliases"]), bool(d["private"]),
                   d["external_id"], d["notes"])

    def to_dict(self):
        return {"id": self.id, "canonical_name": self.canonical_name, "type": self.type, "aliases": list(self.aliases),
                "private": self.private, "external_id": self.external_id, "notes": self.notes}

    @property
    def terms(self):
        """The normalized match terms, canonical name first."""
        return tuple(textmatch.normalize_term(t) for t in (self.canonical_name, *self.aliases))


VERSION = 1


SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


_KEYS = {"id", "canonical_name", "type", "aliases", "private", "external_id", "notes"}


def _clean_text(value, where, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise EntitiesError(f"{where}: {field} must be text")
    return unicodedata.normalize("NFC", value).strip() or None


def _term(raw, where, field):
    try:
        return textmatch.normalize_term(raw)
    except ValueError as e:
        raise EntitiesError(f"{where}: {field} {e}") from None


def make_entity(id, canonical_name, type, aliases=(), private=False, external_id=None, notes=None):
    return Entity(id, canonical_name, type, tuple(aliases), bool(private), external_id, notes).to_dict()


def parse(data, source="entities.json"):
    """Validate decoded JSON; returns normalized entity dicts (file order). Raises EntitiesError."""
    if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
        raise EntitiesError(f"{source}: expected an object with an 'entities' list")
    if data.get("version", VERSION) != VERSION:
        raise EntitiesError(f"{source}: unsupported version {data.get('version')!r} (this code reads version {VERSION})")
    out, ids, owner = [], set(), {}
    for i, raw in enumerate(data["entities"]):
        where = f"{source}: entities[{i}]"
        if not isinstance(raw, dict):
            raise EntitiesError(f"{where} is not an object")
        unknown = sorted(set(raw) - _KEYS)
        if unknown:
            raise EntitiesError(f"{where} has unknown key(s) {unknown}")
        eid = raw.get("id")
        if not isinstance(eid, str) or not SLUG_RE.match(eid):
            raise EntitiesError(f"{where}: id {eid!r} must be a lowercase kebab-case slug")
        where = f"{source}: entity {eid!r}"
        if eid in ids:
            raise EntitiesError(f"{where} is listed twice")
        ids.add(eid)
        name = _clean_text(raw.get("canonical_name"), where, "canonical_name")
        if not name:
            raise EntitiesError(f"{where}: canonical_name is required")
        etype = raw.get("type")
        if etype not in ENTITY_TYPES:
            raise EntitiesError(f"{where}: type {etype!r} must be one of {list(ENTITY_TYPES)}")
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list):
            raise EntitiesError(f"{where}: aliases must be a list of text")
        aliases = [_clean_text(a, where, "alias") for a in aliases]
        if any(a is None for a in aliases):
            raise EntitiesError(f"{where}: an alias is blank")
        private = raw.get("private", False)
        if not isinstance(private, bool):
            raise EntitiesError(f"{where}: private must be true or false")
        e = make_entity(eid, name, etype, aliases, private, _clean_text(raw.get("external_id"), where, "external_id"),
                        _clean_text(raw.get("notes"), where, "notes"))
        for label, field in [(name, "canonical_name"), *[(a, "alias") for a in aliases]]:
            term = _term(label, where, field)
            if term in owner:
                other = owner[term]
                raise EntitiesError(
                    f"{source}: the term {term!r} is claimed twice ({other!r} and {eid!r}"
                    + ("; the same alias is listed twice" if other == eid else "") + "); a name or alias may belong to exactly one entity")
            owner[term] = eid
        out.append(e)
    return out


def to_json(entities):
    """On-disk form: sorted by id, defaults omitted, so diffs in knowledge-private are small."""
    out = []
    for e in sorted(entities, key=lambda e: e["id"]):
        item = {"id": e["id"], "canonical_name": e["canonical_name"], "type": e["type"]}
        if e["aliases"]:
            item["aliases"] = sorted(e["aliases"], key=str.casefold)
        if e["private"]:
            item["private"] = True
        if e["external_id"]:
            item["external_id"] = e["external_id"]
        if e["notes"]:
            item["notes"] = e["notes"]
        out.append(item)
    return {"version": VERSION, "entities": out}


def terms(entity):
    """The entity's normalized match terms, canonical name first."""
    return tuple(textmatch.normalize_term(t) for t in [entity["canonical_name"], *entity["aliases"]])


def private_terms(entities):
    """((canonical_name, terms), ...) for the private entities: what privacy.py matches (raise-only)."""
    return tuple((e["canonical_name"], terms(e)) for e in sorted(entities or [], key=lambda e: e["id"]) if e["private"])


def mentions(text, entity):
    """The terms of `entity` found as whole words in `text` (empty list = no mention)."""
    return textmatch.find_terms(text, terms(entity)) if text else []


def match_all(texts, entities):
    """{entity id: [terms found]} for every entity mentioned anywhere in `texts` (iterable of str/None)."""
    blob = "\n".join(t for t in texts if isinstance(t, str) and t)
    hits = {}
    for e in entities:
        found = mentions(blob, e)
        if found:
            hits[e["id"]] = found
    return hits


def normalize_ref(ref):
    try:
        return textmatch.normalize_term(ref)
    except ValueError:
        return None


def find(entities, ref):
    """The entity a reference names: by id, canonical name or alias (normalized); None if none does."""
    for e in entities:
        if e["id"] == ref:
            return e
    term = normalize_ref(ref)
    if term is None:
        return None
    for e in entities:
        if term in terms(e):
            return e
    return None


def filter_clause(ref, fact_alias="f"):
    """(sql, params) restricting `fact_alias` rows to facts linked to the entity `ref` names (id, canonical
    name or alias, normalized). Raises ValueError for a blank/unmatchable ref. Used by knowledge.py and
    modes.py, so the two query layers cannot disagree; an unknown entity simply matches no facts."""
    term = normalize_ref(ref) if isinstance(ref, str) else None
    if term is None:
        raise ValueError(f"entity {ref!r} must be an entity id, name or alias")
    sql = (f" AND {fact_alias}.id IN (SELECT fe.fact_id FROM fact_entities fe WHERE fe.entity_id IN ("
           "SELECT id FROM entities WHERE name_norm = ? OR entity_key = ? "
           "UNION SELECT entity_id FROM entity_aliases WHERE alias_norm = ?))")
    return sql, [term, ref.strip().lower(), term]


def _copy(entities):
    return [dict(e, aliases=list(e["aliases"])) for e in entities]


def slugify(name):
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return slug or "entity"


def unique_id(name, taken):
    base = slugify(name)
    cand, n = base, 1
    while cand in taken:
        n += 1
        cand = f"{base}-{n}"
    return cand


def add_entity(entities, name, etype="other", aliases=(), private=False, external_id=None, notes=None):
    """Add a new entity; its id is derived from the name. Refused when any of its terms already belongs
    to an entity (the message names which)."""
    if etype not in ENTITY_TYPES:
        raise EntitiesError(f"type {etype!r} must be one of {list(ENTITY_TYPES)}")
    new = _copy(entities)
    new.append(make_entity(unique_id(name or "", {e["id"] for e in entities}), name, etype, aliases, private,
                           external_id, notes))
    parse(to_json(new), source="the edited entities")  # raises on blank name, a collision, a bad alias
    return new, True


def add_alias(entities, ref, alias):
    e = find(entities, ref)
    if e is None:
        raise EntitiesError(f"unknown entity {ref!r}")
    new = _copy(entities)
    target = next(x for x in new if x["id"] == e["id"])
    term = normalize_ref(alias)
    if term is not None and term in terms(target):
        return entities, False
    target["aliases"].append(alias)
    parse(to_json(new), source="the edited entities")
    return new, True


def set_private(entities, ref, private):
    e = find(entities, ref)
    if e is None:
        raise EntitiesError(f"unknown entity {ref!r}")
    if e["private"] == bool(private):
        return entities, False
    new = _copy(entities)
    next(x for x in new if x["id"] == e["id"])["private"] = bool(private)
    return new, True
