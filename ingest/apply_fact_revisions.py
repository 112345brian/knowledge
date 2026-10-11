"""Apply fact_revisions.jsonl (issue #30): validate the append-only revision log, insert
revisions >= 2 into `fact_revisions`, and write each touched fact's latest revision into the
`facts` row (statement, trust, status, visibility, superseded_by_fact_id, recheck, notes).

Revision 1 (the original entry) was already inserted by 04/11. A violation (gap or duplicate
revision number, timestamp going backwards, unknown source_key, unresolvable superseded_by,
malformed line) fails the build and names `file:line`.

Run after the fact-loading steps (04 and 11). A missing or empty log is fine.

The privacy floor (#31) holds after revisions are applied: writing a revision back into `facts`
could otherwise lower a visibility that 04/11 had raised (or miss a name a reworded statement now
contains), so the current rules are re-applied here, raise-only. History is never more visible than
the fact itself: every revision snapshot of a private fact is raised to private too.
"""
import os

import privacy
import revision_apply_service
import revisions


def run(con, ctx):
    deps = revision_apply_service.RevisionApplyDeps(
        revision_log=ctx.stores, revision_table=ctx.stores.fact_revision_table(con),
        privacy_table=ctx.stores.fact_privacy_table(con), rules_reader=ctx.stores)
    done = revision_apply_service.apply_fact_revisions(
        deps, os.path.join(ctx.locations.private_data_dir, revisions.REVISIONS_FILENAME),
        os.path.join(ctx.locations.private_data_dir, privacy.RULES_FILENAME))
    if done.raised:
        print(f"  privacy rules raised {len(done.raised)} fact(s) to private after revisions")
    print(f"[apply_fact_revisions] applied {done.applied} revisions ({done.total_revisions} rows in fact_revisions incl. implicit revision 1)")
