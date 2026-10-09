"""Load entities.json (#42) and link facts to the entities they mention.

Run last: it needs the final fact text (04/11 loaded the facts, 12 applied the revisions) and the privacy
pass that follows each of them. Linking is deterministic literal matching on whole words (entities.py,
textmatch.py), over the same text the privacy resolver scans, so a link and a privacy decision agree.

An absent entities.json is fine (the tables stay empty). A corrupt or invalid file, or a name/alias that
belongs to two entities, fails the build. As a safety net it also fails the build if any fact that mentions
a private entity is not stored private (the privacy pass makes that impossible; this proves it per build).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import entities_store
from paths import PRIVATE_DATA_DIR as DATA_DIR


def run(con):
    found = entities_store.read_file(os.path.join(DATA_DIR, entities_store.ENTITIES_FILENAME))
    if found is None:
        print("[13_link_entities] no entities.json; no entities")
        return
    result = entities_store.link_facts(con, found)
    if result["unprotected"]:
        raise ValueError(f"facts {result['unprotected'][:10]} mention a private entity but are not stored private; "
                         "refusing to build (the privacy pass should have raised them)")
    con.commit()
    private = sum(1 for e in found if e["private"])
    print(f"[13_link_entities] {result['entities']} entities ({private} private), {result['links']} fact links")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
