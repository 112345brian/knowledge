"""Load the subject hierarchy (#43): parents, relation types, descriptions, aliases, deprecations.
Run after 04_ingest_facts.py (subjects are created on the fly during fact ingestion, so they must
exist first) and before 11, which resolves aliases.

Source of truth: `subjects.json` in knowledge-private (see subjects.py for the format and rules).
Every listed subject is created if it is missing (domain from the file, else the schema default); a
subject that already exists keeps the domain it has. An invalid file, an alias that equals a subject
name, or a duplicate alias fails the build.

Until the file exists, the built-in table below is used and a note is printed, so the build keeps
working before `python3 export_subjects.py --apply` has been run and the result committed. After that
the table is dead code and can be deleted (the export tool and its test are the last users).
"""
import sqlite3, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import subjects

AAS_CHILDREN = [
    'aas-cardiovascular-risk', 'aas-cycle-risk', 'aas-decision-framework', 'aas-emergency-red-flags',
    'aas-endocrine', 'aas-estrogen-management', 'aas-injection-safety', 'aas-kidney-toxicity', 'aas-legal',
    'aas-liver-toxicity', 'aas-mental-health', 'aas-post-cycle-therapy', 'aas-side-effects', 'aas-supply-testing',
]
TRAINING_CHILDREN = [
    'training-volume-hypertrophy', 'training-consistency', 'training-detraining', 'training-mental-health',
    'training-mortality-health', 'training-recovery', 'training-scheduling', 'strength-progression-norms',
    'program-design', 'ankle-mobility',
]


def builtin_entries():
    """The hierarchy as it was hardcoded before #43, as subjects.json entries (what export_subjects.py writes)."""
    parents = [subjects._entry("anabolic-steroids"), subjects._entry("training", domain="health-and-fitness")]
    kids = [subjects._entry(n, parent="anabolic-steroids") for n in AAS_CHILDREN]
    kids += [subjects._entry(n, parent="training") for n in TRAINING_CHILDREN]
    return parents + kids


def run_builtin(con):
    """The pre-#43 behavior, unchanged: create 'training', then set the two sets of parents."""
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO subjects (name, domain) VALUES ('training', 'health-and-fitness')")
    cur.execute(
        "UPDATE subjects SET parent_id = (SELECT id FROM subjects WHERE name = 'anabolic-steroids') "
        f"WHERE name IN ({','.join('?' * len(AAS_CHILDREN))})", AAS_CHILDREN
    )
    cur.execute(
        "UPDATE subjects SET parent_id = (SELECT id FROM subjects WHERE name = 'training') "
        f"WHERE name IN ({','.join('?' * len(TRAINING_CHILDREN))})", TRAINING_CHILDREN
    )


def run_entries(con, entries):
    cur = con.cursor()
    for e in entries:
        cur.execute("INSERT OR IGNORE INTO subjects (name, domain) VALUES (?, ?)", (e["name"], e["domain"] or subjects.DEFAULT_DOMAIN))
    ids = {name: i for i, name in cur.execute("SELECT id, name FROM subjects")}
    for e in entries:
        parent = ids[e["parent"]] if e["parent"] else None
        cur.execute(
            "UPDATE subjects SET parent_id = ?, parent_relation = ?, description = ?, deprecated = ? WHERE id = ?",
            (parent, (e["relation"] or subjects.DEFAULT_RELATION) if parent else subjects.DEFAULT_RELATION,
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
    entries = subjects.read_file()  # raises SubjectsError on a bad file: the build fails loudly
    if entries is None:
        run_builtin(con)
        source = "built-in table; subjects.json not found"
    else:
        run_entries(con, entries)
        source = "subjects.json"
    con.commit()
    cur = con.cursor()
    n = cur.execute("SELECT COUNT(*) FROM subjects WHERE parent_id IS NOT NULL").fetchone()[0]
    print(f"[06_seed_subject_hierarchy] {n} subjects now have a parent ({source})")
    if entries is not None:
        a = cur.execute("SELECT COUNT(*) FROM subject_aliases").fetchone()[0]
        d = cur.execute("SELECT COUNT(*) FROM subjects WHERE deprecated = 1").fetchone()[0]
        print(f"  {a} aliases, {d} deprecated subjects")


if __name__ == "__main__":
    con = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.db"))
    con.execute("PRAGMA foreign_keys = ON;")
    run(con)
    con.close()
