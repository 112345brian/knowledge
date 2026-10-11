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
from private_git_port import PrivateGit
import textmatch
from entity_rules import (ENTITY_TYPES, SLUG_RE, VERSION, EntitiesError, Entity, _KEYS, _clean_text, _copy, _term,  # noqa: F401
                          add_alias, add_entity, filter_clause, find, make_entity, match_all, mentions, normalize_ref,
                          parse, private_terms, set_private, slugify, terms, to_json, unique_id)  # (defined with the rules)

from locations import ENTITIES_FILENAME  # noqa: E402  (one definition of the file names, with Locations)


def data_path(data_dir):
    return os.path.join(data_dir, ENTITIES_FILENAME)


def read_file(path):
    """Entities from `path` (an entities.json); None when the file is absent. Corrupt or invalid -> EntitiesError."""
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


# ------------------------------------------------------------------ edits (pure; the caller save()s)
# Each returns (new_entities, changed) and raises EntitiesError for a refusal.


# ------------------------------------------------------------------ the git flow

EditResult = private_edit_store.EditResult


def edit_file(edit, what, message, allow_dirty=False, dry_run=False, *, path, git: PrivateGit):
    """Load the entities file at `path` (an absent file starts empty), apply `edit(entities) -> (new, changed)`,
    and write and commit it through private_edit_store.edit_files (clean tree required unless allow_dirty)."""

    def compute():
        new, changed = edit(read_file(path) or [])
        return changed, new

    return private_edit_store.edit_files([path], compute, lambda new: save(new, path), what, message, git,
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
