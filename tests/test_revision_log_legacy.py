"""A revision log written before `kind`, `valid_from`, `valid_to` and `applies_to` existed (#39, #40, #45) must keep
loading. Such a line says nothing about those fields, so it keeps what the fact had: the original entry's value for
the first logged revision, the previous revision's for later ones, the column defaults when neither is known.
A line with only SOME of the fields is still corrupt. The file is never rewritten; new lines are written in full."""
import json

import pytest

from test_fact_revisions import T1, T2, T3, T4, entry, world  # noqa: F401  (the fixture and helpers)

NEW = ("kind", "valid_from", "valid_to", "applies_to")


def legacy_line(key, revision, at, **kw):
    """A log line exactly as the pre-#39 append_revision wrote it: every mutable field except the four new ones."""
    rec = {"source_key": key, "revision": revision, "changed_at": at, "changed_via": "cli", "session_id": None,
           "change_reason": "because", "statement": "Changed.", "trust_level": "low", "trust_rationale": None,
           "status": "active", "visibility": "private", "superseded_by": None, "recheck_by": None,
           "recheck_rationale": "no decay", "freshness": "no-decay", "notes": None}
    rec.update(kw)
    return json.dumps(rec)


def write_log(w, *lines):
    with open(w.log, "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- the parser

def test_a_legacy_line_inherits_from_the_base_then_from_the_previous_line(world):
    base = {"k1": {"kind": "decision", "valid_from": "2024", "valid_to": None, "applies_to": "adults"}}
    lines = [legacy_line("k1", 2, T2), legacy_line("k1", 3, T3, statement="Again.")]
    recs = [r for _, r in world.rv.parse_log(lines, "log", base)]
    assert [{f: r[f] for f in NEW} for r in recs] == [base["k1"], base["k1"]]


def test_a_legacy_line_with_no_base_gets_the_column_defaults(world):
    (_, rec), = world.rv.parse_log([legacy_line("k1", 2, T2)], "log")
    assert {f: rec[f] for f in NEW} == {"kind": "unclassified", "valid_from": None, "valid_to": None, "applies_to": None}


def test_a_full_line_after_a_legacy_one_is_the_source_for_the_next_legacy_line(world):
    full = json.loads(legacy_line("k1", 3, T3))
    full.update(kind="plan", valid_from="2025", valid_to=None, applies_to=None)
    recs = [r for _, r in world.rv.parse_log([legacy_line("k1", 2, T2), json.dumps(full), legacy_line("k1", 4, T4)], "log")]
    assert [r["kind"] for r in recs] == ["unclassified", "plan", "plan"]


@pytest.mark.parametrize("present", NEW)
def test_a_line_with_only_some_of_the_new_fields_is_still_refused(world, present):
    value = {"kind": "plan", "valid_from": "2025", "valid_to": "2026", "applies_to": "x"}[present]
    with pytest.raises(world.rv.RevisionError, match="missing key"):
        world.rv.parse_log([legacy_line("k1", 2, T2, **{present: value})], "log")


def test_inheritance_never_hides_other_problems(world):
    with pytest.raises(world.rv.RevisionError, match="trust_level"):
        world.rv.parse_log([legacy_line("k1", 2, T2, trust_level="bogus")], "log")
    with pytest.raises(world.rv.RevisionError, match="missing key"):
        world.rv.parse_log([json.dumps({"source_key": "k1", "revision": 2})], "log")


# ---------------------------------------------------------------- end to end through the store

def seed_with_kind(world):
    world.seed(general=[entry("k1", kind="decision", valid_from="2024", applies_to="adults")])


def test_an_old_log_builds_and_keeps_the_entrys_kind(world):
    seed_with_kind(world)
    write_log(world, legacy_line("k1", 2, T2, statement="Edited before the fields existed."))
    con = world.build()
    row = con.execute("SELECT statement, kind, valid_from, applies_to FROM facts").fetchone()
    assert tuple(row) == ("Edited before the fields existed.", "decision", "2024", "adults")
    revs = con.execute("SELECT revision, kind, valid_from, applies_to FROM fact_revisions ORDER BY revision").fetchall()
    assert [tuple(r) for r in revs] == [(1, "decision", "2024", "adults"), (2, "decision", "2024", "adults")]


def test_appending_to_an_old_log_works_and_writes_a_full_line(world):
    seed_with_kind(world)
    write_log(world, legacy_line("k1", 2, T2))
    before = open(world.log).read()
    res = world.append("k1", {"statement": "Third."}, at=T3)
    assert res.ok, res.errors
    assert {f: res.revision[f] for f in NEW} == {"kind": "decision", "valid_from": "2024", "valid_to": None,
                                                 "applies_to": "adults"}
    after = open(world.log).read()
    assert after.startswith(before), "the old lines are never rewritten"
    last = json.loads(after.splitlines()[-1])
    assert list(last) == list(world.rv.REVISION_KEYS) and all(f in last for f in NEW)
    assert world.build().execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 3


def test_current_states_reads_an_old_log(world):
    seed_with_kind(world)
    write_log(world, legacy_line("k1", 2, T2, status="retracted"))
    import review_store
    state = review_store.current_states(world.env.data_dir)["k1"]
    assert (state["status"], state["revision"], state["kind"], state["applies_to"]) == ("retracted", 2, "decision", "adults")


def test_a_mixed_old_and_new_log_round_trips_as_of(world):
    seed_with_kind(world)
    write_log(world, legacy_line("k1", 2, T2))
    assert world.append("k1", {"kind": "plan"}, at=T3).ok
    con = world.build()
    assert [r["kind"] for r in con.execute("SELECT kind FROM fact_revisions ORDER BY revision")] == ["decision", "decision", "plan"]
    import revisions_store
    assert revisions_store.get_fact_as_of(con, 1, T2)["kind"] == "decision"


# ---------------------------------------------------------------- the facts row follows the latest revision

def test_the_facts_row_takes_every_mutable_field_from_the_latest_revision(world):
    """apply_revisions used to write back only the pre-#39 fields, so a revision that changed kind, valid time or
    applies_to showed in `show --as-of` and `history` while `facts --kind` / `--valid-at` kept the entry's values."""
    seed_with_kind(world)
    assert world.append("k1", {"kind": "plan", "valid_from": "2025", "valid_to": "2026-06", "applies_to": "men"}, at=T2).ok
    con = world.build()
    row = con.execute("SELECT kind, valid_from, valid_to, applies_to FROM facts").fetchone()
    assert tuple(row) == ("plan", "2025", "2026-06", "men")
    assert tuple(con.execute("SELECT kind, valid_from, valid_to, applies_to FROM fact_revisions ORDER BY revision DESC").fetchone()) == tuple(row)


def test_clearing_a_field_in_a_revision_clears_it_on_the_facts_row(world):
    seed_with_kind(world)
    assert world.append("k1", {"valid_from": None, "applies_to": None}, at=T2).ok
    row = world.build().execute("SELECT kind, valid_from, applies_to FROM facts").fetchone()
    assert tuple(row) == ("decision", None, None)


def test_search_follows_a_revised_applies_to(world):
    seed_with_kind(world)
    assert world.append("k1", {"applies_to": "postmenopausal women"}, at=T2).ok
    con = world.build()
    assert con.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'postmenopausal'").fetchall()
    assert not con.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'adults'").fetchall()


def test_every_mutable_field_is_written_back_to_the_facts_table(world):
    """The guard that would have caught this: each field a revision can change has a facts column that the apply
    step sets. A new entry in MUTABLE_FIELDS with no write-back fails here."""
    import inspect
    import revisions_store
    src = inspect.getsource(revisions_store.apply_revisions)
    columns = {"superseded_by": "superseded_by_fact_id"}
    for field in world.rv.MUTABLE_FIELDS:
        assert f"{columns.get(field, field)} = ?" in src, f"apply_revisions does not write {field} back to facts"
