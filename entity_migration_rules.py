"""Moving privacy keywords onto private entities (#42): what the migration changes, and the proof it never lowers privacy (pure).

A keyword in privacy_rules.json and a private entity in entities.json both make a fact private when the name
appears as a whole word (same matcher, textmatch.py). An entity adds a type, aliases, notes and a link table, so
the keyword list gets a migration path: each keyword becomes a private entity named by it. Keywords that already
match an existing private entity just disappear from the keyword list; one that matches a NON-private entity is
refused (tag that entity private first), because removing the keyword would lower privacy.

`plan` decides; `entity_migration_store` reads and writes the two files and owns the git flow.
"""
from dataclasses import dataclass, replace
from typing import List, Sequence

import entity_rules
import privacy
from entity_rules import EntitiesError


@dataclass(frozen=True)
class Migration:
    entities: List[dict]          # the full list after the migration, existing entities first
    rules: privacy.Rules          # the rules to write (keywords emptied unless kept)
    added: List[str]              # keywords that became new private entities
    changed: bool


def plan(rules: privacy.Rules, current: Sequence[dict], keep_keywords: bool = False) -> Migration:
    """Convert every keyword in `rules` to a private entity (type 'other'). `keep_keywords` leaves the keyword list
    in place (only adds entities). Raises EntitiesError, and then nothing is to be written."""
    new = [dict(e, aliases=list(e["aliases"])) for e in current]
    added = []
    for kw in rules.keywords:
        owner = entity_rules.find(new, kw)
        if owner is not None:
            if not owner["private"]:
                raise EntitiesError(
                    f"keyword {kw!r} is a name of the non-private entity {owner['id']!r}; tag it private first "
                    "(`entity tag`), otherwise removing the keyword would make its facts less private")
            continue
        new, _ = entity_rules.add_entity(new, kw, "other", private=True)
        added.append(kw)
    new_rules = rules if keep_keywords else replace(rules, keywords=())
    # Prove nothing got less private: every old keyword still raises a mentioning statement.
    after = replace(new_rules, entities=entity_rules.private_terms(new))
    for kw in rules.keywords:
        if privacy.resolve_visibility("any-subject", f"a statement that mentions {kw} somewhere", "normal",
                                      after).visibility != "private":
            raise EntitiesError(f"keyword {kw!r} would no longer make a statement private after the migration; "
                                "nothing was written")
    changed = bool(added) or (not keep_keywords and bool(rules.keywords))
    return Migration(new, new_rules, added, changed)
