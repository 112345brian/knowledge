"""Moving privacy keywords onto private entities (#42). Library only; `knowledge.py entity migrate-keywords` wraps it.

A keyword in privacy_rules.json and a private entity in entities.json both make a fact private when the
name appears as a whole word (same matcher, textmatch.py). An entity adds a type, aliases, notes and a
link table, so the keyword list gets a migration path: each keyword becomes a private entity named by it.

This edits TWO files, so it lives apart from entities.py (which privacy.py imports) and commits them together.
It is safe by construction: before anything is written, every old keyword is re-checked against the NEW state
(the entities, and the rules with the keyword removed) and must still resolve a mentioning statement to private;
the entity file is written first, so there is never a moment with neither. Keywords that already match an
existing private entity just disappear from the keyword list; one that matches a NON-private entity is refused
(tag that entity private first), because removing the keyword would lower privacy.
"""
import os
from dataclasses import replace

import entities_store
import private_edit_store
import privacy
import privacy_store


def migrate_keywords(allow_dirty=False, dry_run=False, keep_keywords=False, data_dir=None):
    """Convert every keyword in the rules file to a private entity (type 'other'). `keep_keywords` leaves the
    keyword list in place (only adds entities). Returns a private_edit_store.EditResult; never prints or exits."""
    rules_file = privacy_store.rules_path(data_dir)
    entities_file = entities_store.data_path(os.path.dirname(rules_file))

    def compute():
        rules = privacy_store.load_rules(rules_file)
        current = entities_store.read_file(entities_file) or []
        new = [dict(e, aliases=list(e["aliases"])) for e in current]
        added = []
        for kw in rules.keywords:
            owner = entities_store.find(new, kw)
            if owner is not None:
                if not owner["private"]:
                    raise entities_store.EntitiesError(
                        f"keyword {kw!r} is a name of the non-private entity {owner['id']!r}; tag it private first "
                        "(`entity tag`), otherwise removing the keyword would make its facts less private")
                continue
            new, _ = entities_store.add_entity(new, kw, "other", private=True)
            added.append(kw)
        new_rules = rules if keep_keywords else replace(rules, keywords=())
        # Prove nothing got less private: every old keyword still raises a mentioning statement.
        after = replace(new_rules, entities=entities_store.private_terms(new))
        for kw in rules.keywords:
            if privacy.resolve_visibility("any-subject", f"a statement that mentions {kw} somewhere", "normal", after).visibility != "private":
                raise entities_store.EntitiesError(f"keyword {kw!r} would no longer make a statement private after the migration; nothing was written")
        changed = bool(added) or (not keep_keywords and bool(rules.keywords))
        return changed, (new, new_rules, added)

    def write(state):
        new, new_rules, _ = state
        entities_store.save(new, entities_file)       # entities first: never a moment with neither
        privacy_store.save_rules(new_rules, rules_file)

    result = private_edit_store.edit_files([entities_file, rules_file], compute, write,
                                     "migrate keywords to private entities", "entities: migrate privacy keywords to private entities",
                                     allow_dirty, dry_run, error_types=(entities_store.EntitiesError, privacy.PrivacyRulesError))
    return result
