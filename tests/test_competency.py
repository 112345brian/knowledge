"""#49: competency questions. docs/competency-questions.md lists the questions the database must answer; this file
runs every answerable one against a fixture db built from the real schema.sql and asserts the SHAPE of the answer, so
a schema change that breaks a question fails here. The meta tests keep the document and the tests in step: every
answerable question names an existing test, every unanswerable one names an issue (or says it is proposed).

The list is a draft until the user signs it off on issue #49.
"""
import os
import re
import sqlite3
import sys

import pytest
from schema_helper import full_schema

from conftest import REPO

DOC = os.path.join(REPO, "docs", "competency-questions.md")
sys.path.insert(0, os.path.join(REPO, "tests"))


@pytest.fixture(autouse=True)
def _restore_modules():
    before = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


def sha(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


class DB(sqlite3.Connection):
    """A connection that can carry the path of the file the fixture facts were extracted from."""


@pytest.fixture
def world(tmp_path):
    """A small knowledge.db with a bit of everything: sources in several states, facts of several kinds and routes, a
    claim with roles, a private entity, revisions, a build stamp, concerts and measurements."""
    note = tmp_path / "note.md"
    note.write_bytes(b"original")
    con = sqlite3.connect(":memory:", factory=DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(full_schema())
    x = con.execute
    x("INSERT INTO subjects (id, name, domain) VALUES (1, 'nutrition', 'health'), (2, 'protein', 'health'), (3, 'family', 'life')")
    x("UPDATE subjects SET parent_id = 1, parent_relation = 'part-of', description = 'Dietary protein' WHERE id = 2")
    x("UPDATE subjects SET deprecated = 1 WHERE id = 3")
    x("INSERT INTO subject_aliases (subject_id, alias) VALUES (2, 'prot')")
    x("INSERT INTO sources (id, citekey, name, source_type, status, status_date, status_note) VALUES "
      "(1, 'smith2020', 'Smith 2020', 'primary', 'active', NULL, NULL), (2, 'old2010', 'Old 2010', 'primary', 'retracted', '2022-05', 'notice'), "
      "(3, 'text1e', 'Textbook 1st ed', 'secondary', 'superseded', '2025', NULL), (4, 'text2e', 'Textbook 2nd ed', 'secondary', 'active', NULL, NULL)")
    x("INSERT INTO source_relations VALUES (4, 'replaces', 3)")
    x("INSERT INTO source_identifiers VALUES (1, 'doi', '10.1210/jc.2007-1692'), (1, 'pmid', '19135656'), (3, 'isbn', '9780306406157')")
    x("INSERT INTO source_identifier_conflicts VALUES (4, 'isbn', '9780306406157', 3)")
    x("INSERT INTO vault_files (id, path, content_sha256) VALUES (1, ?, ?)", (str(note), sha(b"original")))
    cols = "id, subject_id, statement, trust_level, trust_rationale, status, visibility, source_key, freshness, recheck_by, recheck_rationale, kind, valid_from, valid_to, applies_to, captured_via, session_id, captured_at, source_quote, origin_file_id, extracted_from_sha256"
    facts = [
        (1, 2, "Protein at 1.6 g/kg maximizes hypertrophy.", "high", "meta-analysis of 49 trials", "active", "normal", "f-1", "recheck", "2020-01-01", None, "decision", "2019", "2021", "adult men", None, None, None, None, 1, sha(b"original")),
        (2, 1, "Creatine monohydrate does not decay as a fact.", "medium", None, "active", "normal", "f-2", "no-decay", None, "a definition", "definition", None, None, None, "mcp", "sess-1", "2026-10-01T10:00:00+00:00", "creatine is creatine", None, None),
        (3, 1, "Old claim resting on a retracted paper.", "low", None, "active", "normal", "f-3", "recheck", "2099-01-01", None, "observation", None, None, None, "cli", None, "2026-09-01T10:00:00+00:00", None, None, None),
        (4, 1, "Pending item from last night.", "low", None, "pending", "private", "f-4", "recheck", "2099-01-01", None, "plan", None, None, None, "mcp", "sess-1", "2026-10-02T10:00:00+00:00", "quote", None, None),
        (5, 1, "Lunch with Zed Name on Friday.", "low", None, "active", "private", "f-5", "recheck", "2099-01-01", None, "unclassified", None, None, None, "migrate-memory", "sess-2", "2026-10-03T10:00:00+00:00", None, None, None),
        (6, 1, "Example population responds to example intervention.", "medium", None, "active", "normal", "f-6", "recheck", "2099-01-01", None, "observation", None, None, "example population", None, None, None, None, None, None),
        (7, 1, "From the superseded textbook.", "medium", None, "active", "normal", "f-7", "recheck", "2099-01-01", None, "unclassified", None, None, None, None, None, None, None, None, None),
    ]
    for f in facts:
        x(f"INSERT INTO facts ({cols}) VALUES ({','.join('?' * len(f))})", f)
    x("INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES (1, 1, 'p. 12', 'the 1.6 figure'), (3, 2, NULL, NULL), (7, 3, 'ch. 2', NULL)")
    x("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, session_id, change_reason, statement, trust_level, status, visibility, freshness, recheck_by, kind, valid_from, valid_to) VALUES "
      "(1, 'f-1', 1, '2026-01-01T00:00:00+00:00', 'original', NULL, 'original entry', 'Protein at 1.4 g/kg is enough.', 'medium', 'active', 'normal', 'recheck', '2020-01-01', 'decision', '2019', NULL), "
      "(1, 'f-1', 2, '2026-06-01T00:00:00+00:00', 'cli', 's-9', 'new meta-analysis', 'Protein at 1.6 g/kg maximizes hypertrophy.', 'high', 'active', 'normal', 'recheck', '2020-01-01', 'decision', '2019', '2021')")
    x("INSERT INTO claims (id, statement, warrant, qualifier) VALUES (1, 'Eat 1.6 g/kg protein.', 'because the trials agree', 'for adults')")
    x("INSERT INTO claim_facts (claim_id, fact_id, role, note) VALUES (1, 3, 'grounds', 'the old study'), (1, 2, 'backing', NULL), (1, 6, 'rebuttal', 'counter-evidence')")
    x("UPDATE facts SET status = 'retracted' WHERE id = 6")
    x("INSERT INTO entities (id, entity_key, canonical_name, name_norm, type, private) VALUES (1, 'zed-name', 'Zed Name', 'zed name', 'person', 1), (2, 'acme', 'Acme Labs', 'acme labs', 'organization', 0)")
    x("INSERT INTO entity_aliases VALUES (1, 'Zeddy', 'zeddy')")
    x("INSERT INTO fact_entities VALUES (5, 1), (1, 2)")
    x("INSERT INTO build_info (id, built_at, schema_version, code_commit, code_dirty, python_version) VALUES (1, '2026-10-08T00:00:00+00:00', 2, ?, 0, '3.13.5')", ("a" * 40,))
    x("INSERT INTO build_inputs VALUES (1, 'input:concerts-csv', 'present', ?, 5, '2026-01-01T00:00:00+00:00', '2026-10-08T00:00:00+00:00')", ("b" * 64,))
    x("INSERT INTO artists (id, name) VALUES (1, 'Band A'), (2, 'Band B')")
    x("INSERT INTO concert_attendances (artist_id, start_date) VALUES (1, '2024-05-01'), (1, '2025-06-01'), (2, '2025-07-01')")
    x("INSERT INTO metrics (id, key, label, unit) VALUES (1, 'body_fat_pct', 'Body fat', '%')")
    x("INSERT INTO measurements (subject_id, metric_id, value, measured_at, source_id, trust_level) VALUES (1, 1, 20.0, '2026-01-01', 1, 'high'), (1, 1, 18.5, '2026-06-17', 1, 'high')")
    con.commit()
    con.note = note
    return con


# ------------------------------------------------------------------ provenance and trust

def test_cq_01_facts_citing_a_source(world):
    import knowledge
    rows = world.execute("SELECT f.id, fs.locator, fs.quote FROM fact_sources fs JOIN facts f ON f.id = fs.fact_id "
                         "JOIN sources s ON s.id = fs.source_id WHERE s.citekey = 'smith2020'").fetchall()
    assert [tuple(r) for r in rows] == [(1, "p. 12", "the 1.6 figure")]
    assert knowledge.get_fact(world, 1)["sources"] == [{"name": "Smith 2020", "locator": "p. 12"}]


def test_cq_02_trust_and_its_reason(world):
    import knowledge
    f = knowledge.get_fact(world, 1)
    assert (f["trust_level"], f["trust_rationale"]) == ("high", "meta-analysis of 49 trials")


def test_cq_03_capture_provenance(world):
    import knowledge
    f = knowledge.get_fact(world, 2)
    assert (f["captured_via"], f["session_id"], f["captured_at"], f["source_quote"]) == ("mcp", "sess-1", "2026-10-01T10:00:00+00:00", "creatine is creatine")


def test_cq_04_source_file_changed_since_extraction(world):
    import fixity_store
    assert fixity_store.audit_sources(world) == []
    world.note.write_bytes(b"edited later")
    (row,) = fixity_store.audit_sources(world)
    assert (row["fact_id"], row["reason"]) == (1, "changed") and row["baseline_sha256"] != row["current_sha256"]


def test_cq_05_facts_on_retracted_or_replaced_sources(world):
    import source_status_store
    rows = {r["fact_id"]: r for r in source_status_store.audit_source_status(world)}
    assert rows[3]["reason"] == "retracted" and rows[3]["status_date"] == "2022-05"
    assert rows[7]["reason"] == "superseded" and [x["citekey"] for x in rows[7]["replacement"]] == ["text2e"]
    assert 1 not in rows


def test_cq_06_source_identifiers_and_duplicates(world):
    import cli_sources
    import knowledge
    info = cli_sources.source_ids(world, "smith2020")
    assert info["identifiers"] == [{"scheme": "doi", "value": "10.1210/jc.2007-1692"}, {"scheme": "pmid", "value": "19135656"}]
    assert cli_sources.source_ids(world, "text2e")["conflicts"] == [{"scheme": "isbn", "value": "9780306406157", "owner": "text1e"}]
    assert [r["id"] for r in knowledge.list_facts(world, identifier="https://doi.org/10.1210/JC.2007-1692")] == [1]


def test_cq_07_unsupported_facts(world):
    ids = [r[0] for r in world.execute(
        "SELECT f.id FROM facts f WHERE NOT EXISTS (SELECT 1 FROM fact_sources fs WHERE fs.fact_id = f.id) "
        "AND (f.source_quote IS NULL OR f.source_quote = '') ORDER BY f.id")]
    assert ids == [5, 6]                                              # fact 2 has a quote, fact 1 has a source


# ------------------------------------------------------------------ freshness and time

def test_cq_08_facts_due_for_recheck(world):
    rows = world.execute("SELECT id FROM facts WHERE recheck_by GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND recheck_by < '2026-10-08' "
                         "AND status = 'active' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [1]


def test_cq_09_facts_that_never_decay(world):
    rows = world.execute("SELECT id, recheck_rationale FROM facts WHERE freshness = 'no-decay'").fetchall()
    assert [tuple(r) for r in rows] == [(2, "a definition")]


def test_cq_10_facts_true_on_a_date(world):
    import knowledge
    inside = {r["id"] for r in knowledge.list_facts(world, valid_at="2020-06-01")}
    outside = {r["id"] for r in knowledge.list_facts(world, valid_at="2023-01-01")}
    assert 1 in inside and 1 not in outside and 2 in outside                      # a fact with no interval is always in range


def test_cq_11_belief_as_of_a_date(world):
    import revisions_store
    import revisions
    early = revisions_store.get_fact_as_of(world, 1, "2026-03-01")
    late = revisions_store.get_fact_as_of(world, 1, "2026-07-01")
    assert early["statement"].startswith("Protein at 1.4") and late["statement"].startswith("Protein at 1.6")
    assert (early["valid_to"], late["valid_to"]) == (None, "2021")


def test_cq_12_history_of_a_fact(world):
    import revisions_store
    import revisions
    hist = revisions_store.get_history(world, 1)
    assert [h["revision"] for h in hist] == [1, 2] and hist[1]["change_reason"] == "new meta-analysis" and hist[1]["changed_via"] == "cli"


# ------------------------------------------------------------------ premise audit

def test_cq_13_claims_with_stale_premises(world):
    import claims_store
    world.execute("UPDATE facts SET status = 'superseded' WHERE id = 3")
    rows = claims_store.audit_claims(world, "2026-10-08")
    assert [(r["claim_id"], r["fact_id"], r["reason"], r["severity"]) for r in rows if r["role"] == "grounds"] == [(1, 3, "superseded", "weakens")]


def test_cq_14_roles_in_a_claims_argument(world):
    import claims_store
    roles = {r[0]: r[1] for r in world.execute("SELECT fact_id, role FROM claim_facts WHERE claim_id = 1")}
    assert roles == {3: "grounds", 2: "backing", 6: "rebuttal"}
    world.execute("UPDATE facts SET status = 'superseded' WHERE id = 3")
    rows = {r["fact_id"]: r for r in claims_store.audit_claims(world, "2026-10-08")}
    assert rows[6]["severity"] == "info" and rows[6]["reason"] == "rebuttal_retracted" and rows[3]["severity"] == "weakens"


# ------------------------------------------------------------------ privacy

def test_cq_15_why_a_fact_is_private(world):
    import privacy
    rules = privacy.Rules(entities=(("Zed Name", ("zed name", "zeddy")),))
    res = privacy.resolve_visibility("nutrition", "Lunch with Zed Name on Friday.", "normal", rules)
    assert res.visibility == "private" and [r.kind for r in res.raised_by] == ["entity"] and "Zed Name" in res.explain()


def test_cq_16_what_normal_mode_shows(world):
    import modes
    import modes_store
    normal = modes.Session(mode=modes.Mode.normal)
    private = modes.Session(mode=modes.Mode.private)
    shown = {r["id"] for r in modes_store.list_facts(normal, world, limit=100)}
    assert shown == {1, 2, 3, 7} and not ({4, 5} & shown)                          # 4 pending/private, 5 private, 6 retracted
    assert {4, 5} <= {r["id"] for r in modes_store.list_facts(private, world, limit=100, include_pending=True)}


# ------------------------------------------------------------------ capture history

def test_cq_17_pending_and_per_session_captures(world):
    import knowledge
    assert [r["id"] for r in knowledge.list_facts(world, status="pending")] == [4]
    assert [r[0] for r in world.execute("SELECT id FROM facts WHERE session_id = 'sess-1' ORDER BY id")] == [2, 4]


def test_cq_18_capture_routes(world):
    counts = {r[0]: r[1] for r in world.execute("SELECT COALESCE(captured_via, 'original'), COUNT(*) FROM facts GROUP BY 1")}
    assert counts == {"original": 3, "mcp": 2, "cli": 1, "migrate-memory": 1}


# ------------------------------------------------------------------ entities, subjects, applicability, kinds

def test_cq_19_facts_about_an_entity(world):
    import knowledge
    assert [r["id"] for r in knowledge.list_facts(world, entity="Acme Labs")] == [1]
    assert [r["id"] for r in knowledge.list_facts(world, entity="zeddy", status="active")] == [5]            # by alias


def test_cq_20_subject_hierarchy_and_meaning(world):
    import cli_subjects
    rows = {r["name"]: r for r in cli_subjects._rows(world)}
    assert (rows["protein"]["parent"], rows["protein"]["relation"], rows["protein"]["description"]) == ("nutrition", "part-of", "Dietary protein")
    assert rows["protein"]["aliases"] == ["prot"] and rows["family"]["deprecated"] is True and rows["nutrition"]["n_facts"] >= 1


def test_cq_21_facts_for_a_population(world):
    import knowledge
    assert [r["id"] for r in knowledge.search_facts(world, "example population", status="retracted")] == [6]
    assert knowledge.get_fact(world, 1)["applies_to"] == "adult men"


def test_cq_22_facts_of_a_kind(world):
    import knowledge
    assert [r["id"] for r in knowledge.list_facts(world, kind="decision")] == [1]
    assert [r["id"] for r in knowledge.list_facts(world, kind="plan", status="pending")] == [4]


# ------------------------------------------------------------------ build and data

def test_cq_23_which_inputs_produced_this_db(world):
    import build_info_store
    got = build_info_store.latest(world)
    assert got["build"]["schema_version"] == 2 and got["build"]["code_commit"] == "a" * 40
    assert [i["input_key"] for i in got["inputs"]] == ["input:concerts-csv"]


def test_cq_24_most_seen_artists(world):
    rows = world.execute("SELECT a.name, COUNT(*) AS n FROM concert_attendances c JOIN artists a ON a.id = c.artist_id GROUP BY a.id ORDER BY n DESC, a.name").fetchall()
    assert [tuple(r) for r in rows] == [("Band A", 2), ("Band B", 1)]


def test_cq_25_latest_measurement(world):
    row = world.execute("SELECT mt.label, m.value, m.measured_at, s.citekey FROM measurements m JOIN metrics mt ON mt.id = m.metric_id "
                        "JOIN sources s ON s.id = m.source_id WHERE mt.key = 'body_fat_pct' ORDER BY m.measured_at DESC LIMIT 1").fetchone()
    assert tuple(row) == ("Body fat", 18.5, "2026-06-17", "smith2020")


# ------------------------------------------------------------------ the document and the tests stay in step

QUESTION = re.compile(r"^- \*\*(CQ-\d+)\*\* \((\w+)\) (.+)$", re.M)


def parse_doc():
    text = open(DOC, encoding="utf-8").read()
    return text, [(m.group(1), m.group(2), m.group(3)) for m in QUESTION.finditer(text)]


def test_the_document_is_marked_as_a_draft_until_signed_off():
    text, _ = parse_doc()
    assert "DRAFT" in text.split("\n\n", 1)[0] + text[:400] and "#49" in text


def test_there_are_about_twenty_questions_in_several_groups():
    _, qs = parse_doc()
    assert 20 <= len(qs) <= 40
    assert len({g for _, g, _ in qs}) >= 7
    assert len({cq for cq, _, _ in qs}) == len(qs)                                  # ids are unique


def test_every_answerable_question_names_an_existing_test_and_every_other_names_an_issue():
    this = sys.modules[__name__]
    _, qs = parse_doc()
    answered, open_ = 0, 0
    for cq, _, body in qs:
        if " Test: " in body:
            name = body.split(" Test: ", 1)[1].strip()
            assert name.startswith(f"test_cq_{cq[3:]}_") and callable(getattr(this, name, None)), f"{cq} names a missing test {name!r}"
            assert " Answer: " in body, f"{cq} has a test but no stated answer"
            answered += 1
        else:
            assert "Cannot answer yet:" in body, f"{cq} is neither answered by a test nor marked as unanswerable"
            assert re.search(r"See: (#\d+|proposed, not yet filed)", body), f"{cq} is not tied to an issue"
            open_ += 1
    assert answered >= 20 and open_ >= 1


def test_every_cq_test_is_listed_in_the_document():
    this = sys.modules[__name__]
    text, _ = parse_doc()
    tests = [n for n in dir(this) if n.startswith("test_cq_")]
    assert tests and all(n in text for n in tests), [n for n in tests if n not in text]


def test_issues_named_for_unanswerable_questions_are_not_closed_work():
    """#2, #3, #5 (the MCP server) are still open work; this fails the day someone closes them without revisiting CQ-27."""
    text, _ = parse_doc()
    assert "See: #2, #3, #5" in text
