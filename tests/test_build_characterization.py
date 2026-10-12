"""Characterization of the legacy build pipeline: the contract every migrated step must keep.

These tests pin what the steps do TODAY (`run(con, ctx)` modules driven by build.build): the order they run in,
which steps skip and what they say when they do, what each prints and in which order, where each commits, and
what a failure leaves behind. A lane that moves a step behind `build_repositories_port` proves equivalence by
tracing the legacy pipeline and its own bound step over the same world (tests/step_trace.py) and comparing
`lines`, `commits` and `writes_after_last_commit`; the golden dump (test_pipeline_golden) pins the rows.

The expectations below are the observed behaviour, including oddities a lane must keep rather than fix (each is
marked "kept as is"). Regenerate the golden file only for a deliberate behaviour change:
    REGENERATE_STEP_GOLDEN=1 uv run pytest tests/test_build_characterization.py -k golden
"""
import client_bootstrap
import json
import os

import pytest

import step_trace as st
from pipeline_world import REPO

GOLDEN = os.path.join(REPO, "tests", "golden", "step_characterization.json")
ALL_SOURCES = ("concerts", "ratings", "scrobbles", "measurements", "claims")

CORE_ORDER = ["ingest/seed_sources.py", "ingest/literature_sources.py", "ingest/facts.py",
              "ingest/seed_subject_hierarchy.py", "ingest/seed_general_facts.py", "ingest/apply_fact_revisions.py",
              "ingest/link_entities.py", "ingest/link_source_relations.py"]
FULL_ORDER = ["ingest/seed_sources.py", "ingest/literature_sources.py", "client/measurements.py", "ingest/facts.py",
              "client/link_fact_measurements.py", "client/seed_claims.py", "ingest/seed_subject_hierarchy.py",
              "client/concerts.py", "client/music_ratings.py", "client/scrobbles.py", "client/seed_artist_members.py",
              "ingest/seed_general_facts.py", "ingest/apply_fact_revisions.py", "ingest/link_entities.py",
              "ingest/link_source_relations.py"]


def _json(path, value):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    return st.trace_build(tmp_path_factory.mktemp("baseline"), ALL_SOURCES)


@pytest.fixture(scope="module")
def variant(tmp_path_factory):
    """The skip and warning branches the baseline world does not reach, in one build."""
    def mutate(root):
        data = os.path.join(root, "data")
        os.remove(os.path.join(data, "subjects.json"))                 # seed_subject_hierarchy: no file
        os.remove(os.path.join(root, "scrobbles.json"))                # scrobbles: input not found
        _json(os.path.join(data, "entities.json"), {"version": 1, "entities": [
            {"id": "zorblax", "canonical_name": "Zorblax", "type": "organization", "aliases": [], "private": True}]})
        manual = _load(os.path.join(data, "manual_sources.json"))
        manual[0]["doi"] = "10.1000/alpha2020"                          # also the doi of the alpha2020 note
        manual[1]["doi"] = manual[2]["doi"] = "10.5555/shared"          # two manual sources, one doi
        manual[1]["replaces"] = ["source-a"]                            # one source relation
        _json(os.path.join(data, "manual_sources.json"), manual)
        with open(os.path.join(root, "vault", "sources", "source-a.md"), "w", encoding="utf-8") as f:
            f.write("---\ntitle: T\nyear: 2020\nsource-type: primary-research\n---\nbody\n")   # citekey taken
        general = _load(os.path.join(data, "general_facts.json"))
        general[0]["source_citekey"] = "no-such-source"                 # cited but absent
        _json(os.path.join(data, "general_facts.json"), general)
    return st.trace_build(tmp_path_factory.mktemp("variant"), ALL_SOURCES, mutate)


# --------------------------------------------------------------------------------------- order and golden

def test_the_step_order_is_build_rules_steps_for(baseline):
    import build_rules
    assert client_bootstrap.registry().steps_for(ALL_SOURCES) == FULL_ORDER
    assert client_bootstrap.registry().steps_for(()) == CORE_ORDER
    assert baseline.order == FULL_ORDER        # and build.build ran exactly that, once each, in that order


@pytest.mark.parametrize("enabled, extra", [
    ("concerts", ["client/concerts.py", "client/seed_artist_members.py"]),
    ("ratings", ["client/music_ratings.py", "client/seed_artist_members.py"]),
    ("scrobbles", ["client/scrobbles.py", "client/seed_artist_members.py"]),
    ("measurements", ["client/measurements.py", "client/link_fact_measurements.py"]),
    ("claims", ["client/seed_claims.py"]),
])
def test_each_client_source_adds_its_steps_at_fixed_places(enabled, extra):
    import build_rules
    got = client_bootstrap.registry().steps_for((enabled,))
    assert [p for p in got if p not in CORE_ORDER] == extra
    assert [p for p in got if p in CORE_ORDER] == CORE_ORDER      # the core order is the same either way
    assert [p for p in FULL_ORDER if p in got] == got              # and the relative order is the full order's


def test_every_pipeline_path_is_a_module_with_run_con_ctx():
    import ast
    import build_rules
    for path in client_bootstrap.registry().all_steps():
        tree = ast.parse(open(os.path.join(REPO, path), encoding="utf-8").read())
        runs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"]
        assert runs, f"{path} has no module-level run()"
        assert [a.arg for a in runs[0].args.args[:2]] == ["con", "ctx"], path


def test_golden_step_output_commits_and_writes(baseline):
    got = st.summarize(baseline)
    if os.environ.get("REGENERATE_STEP_GOLDEN"):
        with open(GOLDEN, "w", encoding="utf-8") as f:
            json.dump(got, f, indent=1, sort_keys=False)
            f.write("\n")
    assert got == _load(GOLDEN)


def test_every_step_prints_exactly_one_bracketed_summary_line(baseline, variant):
    for trace in (baseline, variant):
        for step in trace.steps:
            name = os.path.splitext(os.path.basename(step.path))[0]
            headlines = [ln for ln in step.lines if ln.startswith(f"[{name}] ")]
            assert len(headlines) == 1, (step.path, step.lines)


# ----------------------------------------------------------------------------------- transaction boundaries

def test_commit_boundaries_per_step(baseline, variant):
    """Each step commits once, after its last write, except where noted. A step that returns before its work
    commits nothing; `apply_fact_revisions` commits twice because the revisions store commits inside
    apply_revisions and the step commits again after the privacy floor (kept as is: a lane may merge them only
    if nothing reads the database between the two)."""
    for step in baseline.steps:
        expected = 2 if step.path == "ingest/apply_fact_revisions.py" else \
            0 if step.path == "ingest/link_entities.py" else 1      # baseline world has no entities.json
        assert step.commits == expected, step.path
        assert not step.writes_after_last_commit, step.path
    assert variant.step("ingest/link_entities.py").commits == 1
    assert variant.step("ingest/seed_subject_hierarchy.py").commits == 0
    assert variant.step("client/scrobbles.py").commits == 0
    for trace in (baseline, variant):
        assert trace.step("ingest/link_source_relations.py").commits == 1   # even when it inserted no relation


def test_steps_do_not_commit_until_they_have_written(baseline):
    """The one commit is the step's last event; nothing is committed half way (a failing step leaves the build
    database to be discarded, see the failure tests)."""
    for step in baseline.steps:
        if step.writes:
            assert step.events[-1] == "commit", step.path
            if step.path != "ingest/apply_fact_revisions.py":
                assert step.events.count("commit") == 1


# ------------------------------------------------------------------------------------------ skip rules

def test_no_subjects_file_skips_the_hierarchy_step_without_writing(variant):
    s = variant.step("ingest/seed_subject_hierarchy.py")
    assert s.lines == ["[seed_subject_hierarchy] no subjects.json: no subject hierarchy loaded"]
    assert (s.commits, s.writes) == (0, 0)


def test_missing_scrobbles_file_skips_without_deleting_or_inserting(variant):
    s = variant.step("client/scrobbles.py")
    assert s.lines == ["[scrobbles] <ROOT>/scrobbles.json not found, skipping"]
    assert (s.commits, s.writes) == (0, 0)


def test_no_entities_file_means_no_entities_and_no_commit(baseline):
    s = baseline.step("ingest/link_entities.py")
    assert s.lines == ["[link_entities] no entities.json; no entities"]
    assert (s.commits, s.writes) == (0, 0)


def test_entities_present_are_loaded_linked_and_committed(variant):
    s = variant.step("ingest/link_entities.py")
    assert s.lines == ["[link_entities] 1 entities (1 private), 1 fact links"]
    assert s.commits == 1


def test_a_note_whose_citekey_exists_is_skipped_but_counted_as_parsed(variant):
    assert variant.step("ingest/literature_sources.py").lines[-1] == \
        "[literature_sources] parsed 6 files, inserted 5 new sources"


def test_identifier_conflict_warnings_come_before_the_summary(variant):
    assert variant.step("ingest/seed_sources.py").lines == [
        "  WARNING -- duplicate identifier doi:10.5555/shared shared by sources 'source-b' and 'source-c' (not merged)",
        "[seed_sources] inserted 6 manual sources"]
    assert variant.step("ingest/literature_sources.py").lines[0] == \
        "  WARNING -- duplicate identifier doi:10.1000/alpha2020 shared by sources 'source-a' and 'alpha2020' (not merged)"


def test_unknown_citekey_is_a_warning_after_the_summary_not_a_failure(variant):
    assert variant.step("ingest/seed_general_facts.py").lines == [
        "[seed_general_facts] inserted 3, skipped 0 (bad shape)",
        "  WARNING -- citekeys referenced but not found in sources: ['no-such-source']"]


def test_relations_are_counted_and_the_status_tally_is_printed(variant):
    assert variant.step("ingest/link_source_relations.py").lines == [
        "[link_source_relations] 1 relations; sources by status: {'active': 22}"]


def test_invalid_items_are_skipped_with_a_warning_before_the_summary(baseline):
    lines = baseline.step("ingest/facts.py").lines
    assert lines[:3] == ["  WARNING -- skipping fact with invalid visibility 'weird': 'Visibility oddity.'",
                         "  WARNING -- skipping fact with invalid status 'odd': 'Status oddity.'",
                         "[facts] inserted 8, skipped 3 (bad shape)"]
    # the detail lines follow in this order: legacy-key note, then the three tallies
    assert [ln.split(":")[0].strip() for ln in lines[3:]] == ["note", "facts by freshness", "facts by trust_level",
                                                                "facts by is_personal"]


def test_the_facts_tally_dicts_are_printed_in_value_order(baseline):
    lines = baseline.step("ingest/facts.py").lines
    assert lines[-3:] == ["  facts by freshness: {'no-decay': 1, 'recheck': 7}",
                          "  facts by trust_level: {'high': 1, 'medium': 7}",
                          "  facts by is_personal: {0: 6, 1: 2}"]    # is_personal keys are ints, kept as is


def test_literature_sources_pins_the_retrieved_date_constant(tmp_path_factory, baseline):
    """Kept as is: the notes step ignores the parsed retrieved_date and writes the literal '2026-09-11'. A lane
    passes that constant through NewSource.retrieved_date unchanged; changing it is a data decision, not a refactor."""
    import sqlite3
    con = sqlite3.connect(baseline.database)
    try:
        dates = {r[0] for r in con.execute("SELECT retrieved_date FROM sources WHERE origin_path LIKE '%sources/%'")}
    finally:
        con.close()
    assert dates == {"2026-09-11"}


# ------------------------------------------------------------------------------------------- failures

def test_a_duplicate_source_key_fails_the_facts_step_after_its_warnings_were_printed(tmp_path_factory):
    def mutate(root):
        _json(os.path.join(root, "data", "facts_batch2.json"), [
            {"source_key": "p-1", "subject": "x", "statement": "dup", "trust_level": "medium",
             "is_original_claim": False, "date_added": "2026-09-11T00:00:00+00:00", "visibility": "private",
             "status": "active", "freshness": "recheck", "recheck_by": "2027-01-01"}])
    trace = st.trace_build(tmp_path_factory.mktemp("dupkey"), ALL_SOURCES, mutate)
    assert trace.failure is not None and trace.failure.stage == "ingest/facts.py"
    assert str(trace.failure.cause) == "duplicate source_key 'p-1' (fact 'dup')"
    facts = trace.step("ingest/facts.py")
    assert facts.error and facts.commits == 0 and facts.writes_after_last_commit    # rows written, never committed
    assert facts.lines == ["  WARNING -- skipping fact with invalid visibility 'weird': 'Visibility oddity.'",
                           "  WARNING -- skipping fact with invalid status 'odd': 'Status oddity.'"]
    assert trace.order == FULL_ORDER[:FULL_ORDER.index("ingest/facts.py") + 1]   # nothing after it ran


def test_a_missing_measurements_snapshot_fails_the_step_before_any_write(tmp_path_factory):
    trace = st.trace_build(tmp_path_factory.mktemp("nosnap"), ALL_SOURCES,
                           lambda root: os.remove(os.path.join(root, "data", "measurements_snapshot.json")))
    assert trace.failure is not None and trace.failure.stage == "client/measurements.py"
    assert "measurements_snapshot.json is missing" in str(trace.failure.cause)
    m = trace.step("client/measurements.py")
    assert (m.commits, m.writes, m.lines) == (0, 0, [])


def test_core_only_build_without_a_notes_vault(tmp_path_factory):
    """No client sources and no NOTES_VAULT: the notes step parses nothing and cited sources are warnings."""
    def mutate(root):
        lp = os.path.join(root, "knowledge-private", "local_paths.py")
        with open(lp, encoding="utf-8") as f:
            kept = [ln for ln in f.read().split("\n") if not ln.startswith("NOTES_VAULT")]
        with open(lp, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
        manual = _load(os.path.join(root, "data", "manual_sources.json"))
        for m in manual:
            m["origin_path"] = None
        _json(os.path.join(root, "data", "manual_sources.json"), manual)
    trace = st.trace_build(tmp_path_factory.mktemp("core"), (), mutate)
    assert trace.failure is None and trace.order == CORE_ORDER
    assert trace.step("ingest/literature_sources.py").lines == \
        ["[literature_sources] parsed 0 files, inserted 0 new sources"]
    assert trace.step("ingest/facts.py").lines[3] == \
        "  WARNING -- citekeys referenced but not found in sources: ['alpha2020']"
