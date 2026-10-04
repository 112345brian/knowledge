"""Cross-feature check written at merge time. #30 (revisions) and #31 (privacy rules) were built
separately: ingest applied the privacy floor, then step 12 wrote each fact's latest revision back
into `facts`, which could lower visibility again. The floor must hold after revisions are applied,
and revision history must never be more visible than the fact itself. Synthetic names only."""
import json
import os

from test_fact_revisions import world, entry, T2, T3  # noqa: F401  (fixture import)


def write_rules(world, tags=None, keywords=None):
    with open(os.path.join(world.env.data_dir, "privacy_rules.json"), "w") as f:
        json.dump({"version": 1, "subject_tags": tags or {}, "keywords": keywords or []}, f)


def facts_visibility(con):
    return {r[0]: r[1] for r in con.execute("SELECT source_key, visibility FROM facts")}


def test_a_revision_saying_normal_cannot_lower_a_subject_tagged_private(world):
    write_rules(world, tags={"alpha": "private"})
    world.seed(general=[entry("k1", visibility="normal")])
    assert world.append("k1", {"visibility": "normal", "trust_level": "high"}, "reviewed", at=T2).ok
    con = world.build()
    assert facts_visibility(con)["k1"] == "private"


def test_a_revision_that_rewords_a_fact_to_contain_a_listed_name_is_privatized(world):
    write_rules(world, keywords=["quenby"])
    world.seed(general=[entry("k1", visibility="normal", statement="A harmless statement.")])
    assert world.append("k1", {"statement": "Quenby came to visit."}, "reworded", at=T2).ok
    con = world.build()
    assert facts_visibility(con)["k1"] == "private"


def test_history_is_never_more_visible_than_the_current_fact(world):
    write_rules(world, tags={"alpha": "private"})
    world.seed(general=[entry("k1", visibility="normal")])
    assert world.append("k1", {"trust_level": "high"}, "one", at=T2).ok
    assert world.append("k1", {"trust_level": "medium"}, "two", at=T3).ok
    con = world.build()
    assert {h["visibility"] for h in world.rv.get_history(con, "k1")} == {"private"}


def test_without_rules_a_normal_revision_still_lowers_nothing_it_should_not(world):
    # No rules file: the unknown-subject rule does not apply in the build (no subjects context
    # beyond the db), and an explicit normal stays normal; revisions keep working as before.
    world.seed(general=[entry("k1", visibility="private")])
    assert world.append("k1", {"visibility": "normal"}, "promote", at=T2).ok
    con = world.build()
    assert facts_visibility(con)["k1"] == "normal"
