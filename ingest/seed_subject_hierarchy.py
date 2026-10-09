"""Load the subject hierarchy (#43): parents, relation types, descriptions, aliases, deprecations.
Run after facts.py (subjects are created on the fly during fact ingestion, so they must
exist first) and before 11, which resolves aliases.

Source of truth: `subjects.json` in knowledge-private (see subjects.py for the format and rules).
Every listed subject is created if it is missing (domain from the file, else the schema default); a
subject that already exists keeps the domain it has. An invalid file, an alias that equals a subject
name, or a duplicate alias fails the build.

Until the file exists no hierarchy is loaded and a note says so. (A checkout that enables the `claims` client
source gets the author's built-in tree from client/seed_subject_tree.py until it has run
`python3 -m client.export_subjects --apply` and committed the result.)
"""
import sqlite3, os, sys

import subjects_store


def run_entries(con, entries):
    cur = con.cursor()
    for e in entries:
        cur.execute("INSERT OR IGNORE INTO subjects (name, domain) VALUES (?, ?)", (e["name"], e["domain"] or subjects_store.DEFAULT_DOMAIN))
    ids = {name: i for i, name in cur.execute("SELECT id, name FROM subjects")}
    for e in entries:
        parent = ids[e["parent"]] if e["parent"] else None
        cur.execute(
            "UPDATE subjects SET parent_id = ?, parent_relation = ?, description = ?, deprecated = ? WHERE id = ?",
            (parent, (e["relation"] or subjects_store.DEFAULT_RELATION) if parent else subjects_store.DEFAULT_RELATION,
             e["description"], 1 if e["deprecated"] else 0, ids[e["name"]]))
    for e in entries:  # second pass: the replacement may be listed after the subject
        if e["replaced_by"]:
            cur.execute("UPDATE subjects SET replaced_by_subject_id = ? WHERE id = ?", (ids[e["replaced_by"]], ids[e["name"]]))
        for alias in e["aliases"]:
            try:
                cur.execute("INSERT INTO subject_aliases (subject_id, alias) VALUES (?, ?)", (ids[e["name"]], alias))
            except sqlite3.IntegrityError as err:
                raise ValueError(f"subjects.json: alias {alias!r} of {e['name']!r} cannot be loaded ({err}); "
                                 "it equals an existing subject name or another alias") from None


def run(con):
    entries = subjects_store.read_file()  # raises SubjectsError on a bad file: the build fails loudly
    if entries is None:
        print("[seed_subject_hierarchy] no subjects.json: no subject hierarchy loaded")
        return
    run_entries(con, entries)
    source = "subjects.json"
    con.commit()
    cur = con.cursor()
    n = cur.execute("SELECT COUNT(*) FROM subjects WHERE parent_id IS NOT NULL").fetchone()[0]
    print(f"[seed_subject_hierarchy] {n} subjects now have a parent ({source})")
    if entries is not None:
        a = cur.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0]
        d = cur.execute("SELECT COUNT(*) FROM subjects WHERE deprecated = 1").fetchone()[0]
        print(f"  {a} aliases, {d} deprecated subjects")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
