"""Load entities.json (#42) and link facts to the entities they mention.

Run last: it needs the final fact text (04/11 loaded the facts, 12 applied the revisions) and the privacy
pass that follows each of them. Linking is deterministic literal matching on whole words (entities.py,
textmatch.py), over the same text the privacy resolver scans, so a link and a privacy decision agree.

An absent entities.json is fine (the tables stay empty). A corrupt or invalid file, or a name/alias that
belongs to two entities, fails the build. As a safety net it also fails the build if any fact that mentions
a private entity is not stored private (the privacy pass makes that impossible; this proves it per build).
"""
import entity_link_service


def run(con, ctx):
    repository = ctx.stores.entity_repository(con)
    found = repository.read_file(ctx.locations.entities_file())
    if found is None:
        print("[link_entities] no entities.json; no entities")
        return
    deps = entity_link_service.EntityLinkDeps(repository)
    result = entity_link_service.link_entities(deps, found)   # refuses a private mention that is not stored private
    con.commit()
    private = sum(1 for e in found if e.private)
    print(f"[link_entities] {result.entities} entities ({private} private), {result.links} fact links")
