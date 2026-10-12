"""Load the subject hierarchy (#43): parents, relation types, descriptions, aliases, deprecations.
Run after facts.py (subjects are created on the fly during fact ingestion, so they must
exist first) and before 11, which resolves aliases.

Source of truth: `subjects.json` in knowledge-private (see subjects.py for the format and rules).
Every listed subject is created if it is missing (domain from the file, else the schema default); a
subject that already exists keeps the domain it has. An invalid file, an alias that equals a subject
name, or a duplicate alias fails the build.

Until the file exists no hierarchy is loaded and a note says so.
"""
import subject_hierarchy_service


def run(con, ctx):
    subjects = ctx.stores.subject_repository(con)
    entries = subjects.read_file(ctx.locations.subjects_file())  # raises SubjectsError on a bad file: the build fails loudly
    if entries is None:
        print("[seed_subject_hierarchy] no subjects.json: no subject hierarchy loaded")
        return
    deps = subject_hierarchy_service.HierarchyDeps(subjects=subjects)
    tally = subject_hierarchy_service.load_hierarchy(deps, entries)
    con.commit()
    print(f"[seed_subject_hierarchy] {tally.with_parent} subjects now have a parent (subjects.json)")
    print(f"  {tally.aliases} aliases, {tally.deprecated} deprecated subjects")
