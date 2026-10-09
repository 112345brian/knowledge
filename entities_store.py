"""Entities as first-class rows (#42): the people, organizations, places, projects and substances facts are about.
Library only; `knowledge.py entity ...` wraps the edits, step 13 builds the tables.

`entities.json` in knowledge-private (it holds personal names, so never in the public repo), next to
privacy_rules.json:

    {"version": 1, "entities": [
        {"id": "mary-jane", "canonical_name": "Mary Jane", "type": "person",
         "aliases": ["MJ", "Mary J."], "private": true, "external_id": "wikidata:Q42", "notes": "..."}, ...]}

Required: id (lowercase kebab slug, unique), canonical_name, type (person | organization | place | project |
substance | other). Optional: aliases, private (default false), external_id, notes. Unknown keys are refused.

Linking is deterministic literal matching, with no model and no NER: a fact is linked to an entity when the
canonical name or an alias appears as a WHOLE word, case-insensitively, NFC-normalized, in the fact's text
(textmatch.py, the very code the privacy keyword list uses). "brother" does not match "brotherhood". The text
scanned is the statement plus every other stored free-text field of the fact (the same set the privacy
resolver scans), so a link and a privacy decision can never disagree about what a fact mentions.

Every canonical name and alias is unique across ALL entities after normalization (casefold, whitespace
collapsed): one term cannot belong to two entities. A collision fails the build and the edit that causes it.

A `private` entity makes every fact that mentions it private (privacy.py reads `private_terms`).
"""
import contextlib
import json
import os
import re
import stat
import unicodedata
import uuid

import private_edit_store
import textmatch

ENTITIES_FILENAME = "entities.json"
VERSION = 1
ENTITY_TYPES = ("person", "organization", "place", "project", "substance", "other")  # keep in sync with schema.sql
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_KEYS = {"id", "canonical_name", "type", "aliases", "private", "external_id", "notes"}


class EntitiesError(Exception):
    """entities.json is unreadable or breaks a rule. The message is safe to show as is."""


def data_path(data_dir=None):
    if data_dir is None:
        from paths import PRIVATE_DATA_DIR  # lazy: this module imports without a private repo
        data_dir = PRIVATE_DATA_DIR
    return os.path.join(data_dir, ENTITIES_FILENAME)


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
    return {"id": id, "canonical_name": canonical_name, "type": type, "aliases": list(aliases),
            "private": bool(private), "external_id": external_id, "notes": notes}


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


def read_file(path=None):
    """Entities from `path` (default: the private data dir's entities.json); None when the file is absent.
    Corrupt or invalid -> EntitiesError."""
    path = data_path() if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise EntitiesError(f"{path} is not valid JSON ({e}); left untouched") from e
    except (OSError, UnicodeDecodeError) as e:
        raise EntitiesError(f"could not read {path}: {e}") from e
    return parse(data, source=path)


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


def save(entities, path):
    """Atomic write (temp file + os.replace); parses what it is about to write, so it never writes a file
    the loader would refuse."""
    data = to_json(entities)
    parse(data, source="entities to write")
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


# ------------------------------------------------------------------ terms and matching

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


# ------------------------------------------------------------------ edits (pure; the caller save()s)
# Each returns (new_entities, changed) and raises EntitiesError for a refusal.

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


# ------------------------------------------------------------------ the git flow

EditResult = private_edit_store.EditResult


def edit_file(edit, what, message, allow_dirty=False, dry_run=False, path=None):
    """Load entities.json (an absent file starts empty), apply `edit(entities) -> (new, changed)`, and write
    and commit it through private_edit_store.edit_files (clean tree required unless allow_dirty)."""
    path = data_path() if path is None else path

    def compute():
        new, changed = edit(read_file(path) or [])
        return changed, new

    return private_edit_store.edit_files([path], compute, lambda new: save(new, path), what, message,
                                   allow_dirty, dry_run, error_types=(EntitiesError,))


# ------------------------------------------------------------------ linking (used by build step 13)

_FACT_TEXT_SQL = """
    SELECT f.id, f.statement, f.notes, f.trust_rationale, f.recheck_rationale, f.source_quote, f.applies_to,
           (SELECT group_concat(COALESCE(fs.locator, '') || ' ' || COALESCE(fs.quote, ''), char(10))
            FROM fact_sources fs WHERE fs.fact_id = f.id)
    FROM facts f ORDER BY f.id"""


def link_facts(con, entities):
    """Insert `entities` and their aliases, then a fact_entities row per (fact, mentioned entity), into a
    built db. Returns {"entities": n, "links": n, "unprotected": [fact ids]}: the last is an invariant
    check, facts that mention a private entity but are not stored private (the privacy pass should have
    made that impossible; the caller fails the build if the list is not empty). Does not commit."""
    cur = con.cursor()
    row_ids = {}
    for e in entities:
        cur.execute("INSERT INTO entities (entity_key, canonical_name, name_norm, type, private, external_id, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (e["id"], e["canonical_name"], textmatch.normalize_term(e["canonical_name"]), e["type"],
                     1 if e["private"] else 0, e["external_id"], e["notes"]))
        row_ids[e["id"]] = cur.lastrowid
        for a in e["aliases"]:
            cur.execute("INSERT INTO entity_aliases (entity_id, alias, alias_norm) VALUES (?, ?, ?)",
                        (row_ids[e["id"]], a, textmatch.normalize_term(a)))
    private_ids = {e["id"] for e in entities if e["private"]}
    links, unprotected = 0, []
    for fact_id, *texts in con.execute(_FACT_TEXT_SQL).fetchall():
        hits = match_all(texts, entities)
        for eid in hits:
            cur.execute("INSERT INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fact_id, row_ids[eid]))
            links += 1
        if private_ids & set(hits):
            if cur.execute("SELECT visibility FROM facts WHERE id = ?", (fact_id,)).fetchone()[0] != "private":
                unprotected.append(fact_id)
    return {"entities": len(entities), "links": links, "unprotected": unprotected}
