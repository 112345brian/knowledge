"""The author's built-in subject tree (the `claims` client source): `anabolic-steroids` and `training` with their
children, as it was hardcoded before the hierarchy became data (#43).

Used only while there is no subjects.json: `python3 -m client.export_subjects --apply` writes this same tree
to subjects.json, and once that file is committed this step does nothing and can be dropped from CLIENT_SOURCES.
Runs right after the core seed_subject_hierarchy step, which loads subjects.json when it exists.
"""
import subjects_store

from client.seed_rules import AAS_CHILDREN, TRAINING_CHILDREN


def builtin_entries():
    """The hierarchy as it was hardcoded before #43, as subjects.json entries (what export_subjects.py writes)."""
    parents = [subjects_store._entry("anabolic-steroids"), subjects_store._entry("training", domain="health-and-fitness")]
    kids = [subjects_store._entry(n, parent="anabolic-steroids") for n in AAS_CHILDREN]
    kids += [subjects_store._entry(n, parent="training") for n in TRAINING_CHILDREN]
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


def run(con):
    if subjects_store.read_file() is not None:
        print("[seed_subject_tree] subjects.json present: the built-in tree is not used")
        return
    run_builtin(con)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM subjects WHERE parent_id IS NOT NULL").fetchone()[0]
    print(f"[seed_subject_tree] {n} subjects now have a parent (built-in tree; subjects.json not found)")
