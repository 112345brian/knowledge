"""Adapter for the fact lifecycle (#8, #23, #33): what the decisions need from the outside world.
The rules file and the db's subject tree (for lowering visibility), and the subjects named in the
entry files. The rules are in `lifecycle_rules`; the use case is `lifecycle`.
"""
import sqlite3

import privacy
import privacy_store
import revisions_store

DB_ERRORS = (sqlite3.Error,)


def floor_inputs(data_dir, db):
    """((rules, {subject: parent}) | a refusal message) for checking whether a fact may be lowered to normal."""
    if db is None:
        return ("lowering to normal needs the built db (for the subject tree the privacy rules apply to); "
                "build it, or pass db=")
    try:
        rules = privacy_store.load_rules(privacy_store.rules_path(data_dir))
        with revisions_store.connection(db) as con:
            rows = con.execute("SELECT s.name, p.name FROM subjects s LEFT JOIN subjects p ON p.id = s.parent_id").fetchall()
    except privacy.PrivacyRulesError as e:
        return f"cannot check the privacy rules: {e}"
    except sqlite3.Error as e:
        return f"cannot read the subject tree from the db: {e} (rebuild it)"
    return rules, {n: p for n, p in rows}


def subjects(data_dir):
    """{source_key: subject} from the entry files."""
    return {e["key"]: e["entry"].get("subject") for e in revisions_store.load_entries(data_dir)}
