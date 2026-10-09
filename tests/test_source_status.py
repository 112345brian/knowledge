"""#41: source status over time, editions, source-to-source relations, and the fact -> source audit.

Pins what 01/02 do with legacy sources first (counts, columns, status 'active' with a NULL date), then the new
fields from frontmatter and manual_sources.json, relation validation (unknown citekey, self, cycles), the
audit in every status, the CLI, and the normal-only DB. Temp dirs only.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

import pytest

import source_status as ss
from test_add_fact import REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _restore_modules():
    before = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


def load(name, tag="m"):
    spec = importlib.util.spec_from_file_location(f"{tag}_{name}", os.path.join(REPO, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def note(frontmatter, body="body"):
    lines = ["---"] + [f"{k}: {v}" if not isinstance(v, list) else f"{k}:\n" + "\n".join(f'  - "{x}"' for x in v)
                        for k, v in frontmatter.items()] + ["---", body, ""]
    return "\n".join(lines)


def write_vault(tmp_path, notes):
    d = tmp_path / "vault" / "sources"
    d.mkdir(parents=True)
    for name, fm in notes.items():
        (d / f"{name}.md").write_text(note({"title": name, "year": "2020", "source-type": "peer-reviewed-study", **fm}))
    (d / "README.md").write_text("skipped")
    return str(d)


def new_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    return con


def run02(ingest, tmp_path, notes, con=None):
    mod = load("02_ingest_literature_sources.py")
    mod.SRC_DIR = write_vault(tmp_path, notes)
    con = con or ingest.db()
    mod.run(con)
    return con, mod


# ------------------------------------------------------------------ pin: legacy ingest

def test_02_legacy_notes_are_active_with_a_null_status_date_and_counts_are_unchanged(ingest, tmp_path):
    con, _ = run02(ingest, tmp_path, {"a2020": {}, "b2021": {"edition": "2nd edition"}})
    assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
    rows = {r["citekey"]: r for r in con.execute("SELECT * FROM sources")}
    for r in rows.values():
        assert (r["status"], r["status_date"], r["status_note"], r["original_published_date"]) == ("active", None, None, None)
    assert (rows["a2020"]["edition"], rows["b2021"]["edition"]) == (None, "2nd edition")
    assert con.execute("SELECT COUNT(*) FROM source_relations").fetchone()[0] == 0


def test_01_manual_sources_without_status_fields_are_active(ingest):
    with open(os.path.join(ingest.env.data_dir, "manual_sources.json"), "w") as f:
        json.dump([{"citekey": "m1", "name": "Manual one", "source_type": "primary", "published_date": "2026-01-01"}], f)
    mod = load("01_seed_sources.py")
    con = ingest.db()
    mod.run(con)
    r = con.execute("SELECT * FROM sources").fetchone()
    assert (r["status"], r["status_date"]) == ("active", None)


# ------------------------------------------------------------------ the new fields

def test_02_reads_status_fields_and_relations_from_frontmatter(ingest, tmp_path):
    con, mod = run02(ingest, tmp_path, {
        "old2015": {"source-status": "retracted", "source-status-date": "2019-07", "source-status-note": "https://example.org/notice",
                    "original-published-date": "2014-11-02"},
        "new2020": {"replaces": ["old2015"], "edition": "3rd edition"},
    })
    r = {x["citekey"]: x for x in con.execute("SELECT * FROM sources")}
    assert (r["old2015"]["status"], r["old2015"]["status_date"], r["old2015"]["status_note"], r["old2015"]["original_published_date"]) == \
        ("retracted", "2019-07", "https://example.org/notice", "2014-11-02")
    rows = mod.parse_all()
    assert [x["relations"] for x in rows if x["citekey"] == "new2020"] == [[("new2020", "replaces", "old2015")]]


@pytest.mark.parametrize("fm, msg", [
    ({"source-status": "deleted"}, "status 'deleted' must be one of"),
    ({"source-status-date": "March 2019"}, "status_date"),
    ({"source-status-date": "2019-02-30"}, "status_date"),
])
def test_02_a_bad_status_or_date_fails_the_build_naming_the_note(ingest, tmp_path, fm, msg):
    with pytest.raises(Exception, match=msg) as e:
        run02(ingest, tmp_path, {"bad2020": fm})
    assert "bad2020" in str(e.value) and type(e.value).__name__ == "SourceStatusError"


def test_01_reads_status_fields_from_manual_sources_json(ingest):
    with open(os.path.join(ingest.env.data_dir, "manual_sources.json"), "w") as f:
        json.dump([{"citekey": "m1", "name": "M", "source_type": "primary", "status": "expression-of-concern",
                    "status_date": "2024", "status_note": "see journal", "edition": "1st", "original_published_date": "1999"}], f)
    con = ingest.db()
    load("01_seed_sources.py").run(con)
    r = con.execute("SELECT * FROM sources").fetchone()
    assert (r["status"], r["status_date"], r["status_note"], r["edition"], r["original_published_date"]) == \
        ("expression-of-concern", "2024", "see journal", "1st", "1999")
    with open(os.path.join(ingest.env.data_dir, "manual_sources.json"), "w") as f:
        json.dump([{"citekey": "m2", "name": "M", "source_type": "primary", "status": "banned"}], f)
    with pytest.raises(Exception, match="m2") as e:
        load("01_seed_sources.py").run(ingest.db())
    assert type(e.value).__name__ == "SourceStatusError"


def test_normalize_fields():
    assert ss.normalize_fields({}, "w") == {"status": "active", "status_date": None, "status_note": None, "edition": None, "original_published_date": None}
    out = ss.normalize_fields({"status": "  retracted ", "status_date": " 2020 ", "edition": "  ", "status_note": ""}, "w")
    assert (out["status"], out["status_date"], out["edition"], out["status_note"]) == ("retracted", "2020", None, None)
    assert ss.normalize_fields({"status": ""}, "w")["status"] == "active"
    for s in ss.STATUS_VALUES:
        assert ss.normalize_fields({"status": s}, "w")["status"] == s
    for bad in ({"status": 5}, {"status": "Retracted"}, {"status_date": "x"}, {"status_date": 2020}, {"edition": 2}):
        with pytest.raises(ss.SourceStatusError):
            ss.normalize_fields(bad, "w")


def test_as_list():
    assert ss.as_list(None) == [] and ss.as_list([]) == [] and ss.as_list("") == []
    assert ss.as_list(["[[a]]", "b", " c "]) == ["a", "b", "c"]
    assert ss.as_list("[a, b]") == ["a", "b"] and ss.as_list("a, b") == ["a", "b"] and ss.as_list("a") == ["a"]
    with pytest.raises(ss.SourceStatusError):
        ss.as_list([1])


# ------------------------------------------------------------------ relation validation

def test_check_relations_accepts_chains_and_collapses_duplicates():
    known = {"a", "b", "c"}
    rels = [("c", "replaces", "b"), ("b", "replaces", "a"), ("c", "replaces", "b"), ("c", "is-version-of", "a")]
    assert ss.check_relations(rels, known) == [("c", "replaces", "b"), ("b", "replaces", "a"), ("c", "is-version-of", "a")]
    assert ss.check_relations([], known) == []


@pytest.mark.parametrize("rels, msg", [
    ([("a", "replaces", "zz")], "no source has the citekey 'zz'"),
    ([("zz", "replaces", "a")], "no source has the citekey 'zz'"),
    ([("a", "obsoletes", "b")], "relation 'obsoletes'"),
    ([("a", "replaces", "a")], "cannot replaces itself|cannot replace itself"),
    ([("a", "replaces", "b"), ("b", "replaces", "a")], "replaces cycle among sources: a -> b -> a|replaces cycle among sources: b -> a -> b"),
    ([("a", "replaces", "b"), ("b", "replaces", "c"), ("c", "replaces", "a")], "replaces cycle among sources"),
    ([("a", "is-version-of", "b"), ("b", "is-version-of", "a")], "is-version-of cycle among sources"),
])
def test_check_relations_refuses(rels, msg):
    with pytest.raises(ss.SourceStatusError, match=msg):
        ss.check_relations(rels, {"a", "b", "c"})


def test_different_relation_types_do_not_form_a_cycle_together():
    assert len(ss.check_relations([("a", "replaces", "b"), ("b", "is-version-of", "a")], {"a", "b"})) == 2


def test_cycle_message_names_every_source_in_it():
    with pytest.raises(ss.SourceStatusError) as e:
        ss.check_relations([("x1", "replaces", "x2"), ("x2", "replaces", "x3"), ("x3", "replaces", "x1")], {"x1", "x2", "x3"})
    assert all(k in str(e.value) for k in ("x1", "x2", "x3"))


def test_schema_checks():
    con = new_db()
    con.execute("INSERT INTO sources (id, citekey, name, source_type) VALUES (1, 'a', 'A', 'primary'), (2, 'b', 'B', 'primary')")
    for bad in ("deleted", "Active", ""):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE sources SET status = ? WHERE id = 1", (bad,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE sources SET status_date = 'March' WHERE id = 1")
    con.execute("UPDATE sources SET status = 'retracted', status_date = '2020-03' WHERE id = 1")
    con.execute("INSERT INTO source_relations VALUES (2, 'replaces', 1)")
    for sql in ("INSERT INTO source_relations VALUES (2, 'replaces', 1)", "INSERT INTO source_relations VALUES (1, 'replaces', 1)",
                "INSERT INTO source_relations VALUES (1, 'obsoletes', 2)", "INSERT INTO source_relations VALUES (1, 'replaces', 99)"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sql)


# ------------------------------------------------------------------ step 14

def run14(ingest, tmp_path, notes, manual=None):
    con, mod02 = run02(ingest, tmp_path, notes)
    if manual is not None:
        with open(os.path.join(ingest.env.data_dir, "manual_sources.json"), "w") as f:
            json.dump(manual, f)
        load("01_seed_sources.py").run(con)
    mod14 = load("14_link_source_relations.py")
    real = mod14._load

    def patched(filename):
        m = real(filename)
        if filename.startswith("02_"):
            m.SRC_DIR = mod02.SRC_DIR
        return m
    mod14._load = patched
    if manual is None:
        with open(os.path.join(ingest.env.data_dir, "manual_sources.json"), "w") as f:
            json.dump([], f)
    return con, mod14


def test_step14_loads_relations_from_notes_and_manual_sources(ingest, tmp_path, capsys):
    con, mod14 = run14(ingest, tmp_path,
                       {"old2015": {}, "mid2018": {"replaces": ["old2015"]}, "pre2014": {"is-version-of": "[old2015]"}},
                       manual=[{"citekey": "textbook-3e", "name": "3rd ed", "source_type": "secondary", "edition": "3rd",
                                "replaces": ["mid2018"]}])
    mod14.run(con)
    rels = {(a, r, b) for a, r, b in con.execute(
        "SELECT s.citekey, r.relation, t.citekey FROM source_relations r JOIN sources s ON s.id = r.source_id JOIN sources t ON t.id = r.related_source_id")}
    assert rels == {("mid2018", "replaces", "old2015"), ("pre2014", "is-version-of", "old2015"), ("textbook-3e", "replaces", "mid2018")}
    assert "3 relations" in capsys.readouterr().out


def test_step14_with_no_relations_is_fine(ingest, tmp_path, capsys):
    con, mod14 = run14(ingest, tmp_path, {"a2020": {}})
    mod14.run(con)
    assert "0 relations" in capsys.readouterr().out and con.execute("SELECT COUNT(*) FROM source_relations").fetchone()[0] == 0


@pytest.mark.parametrize("notes, msg", [
    ({"a2020": {"replaces": ["ghost2000"]}}, "ghost2000"),
    ({"a2020": {"replaces": ["a2020"]}}, "itself"),
    ({"a2020": {"replaces": ["b2021"]}, "b2021": {"replaces": ["a2020"]}}, "cycle among sources"),
])
def test_step14_refuses_bad_relations_naming_the_sources(ingest, tmp_path, notes, msg):
    con, mod14 = run14(ingest, tmp_path, notes)
    with pytest.raises(Exception, match=msg) as e:
        mod14.run(con)
    assert type(e.value).__name__ == "SourceStatusError"
    assert con.execute("SELECT COUNT(*) FROM source_relations").fetchone()[0] == 0


# ------------------------------------------------------------------ the audit

def db_with_sources():
    """Sources: 1 active, 2 retracted, 3 expression-of-concern, 4 old (status superseded), 5 mid (superseded), 6 newest (active),
    7 corrected, 8 superseded with nothing replacing it, 9 active but replaced by 10 (derived), 10 active.
    6 replaces 5 replaces 4; 10 replaces 9."""
    con = new_db()
    con.execute("INSERT INTO subjects (id, name) VALUES (1, 's')")
    spec = [(1, "act", "active"), (2, "ret", "retracted"), (3, "eoc", "expression-of-concern"), (4, "old", "superseded"),
            (5, "mid", "superseded"), (6, "new", "active"), (7, "cor", "corrected"), (8, "orphan", "superseded"),
            (9, "quiet", "active"), (10, "quiet2", "active")]
    for i, key, status in spec:
        con.execute("INSERT INTO sources (id, citekey, name, source_type, status, status_date, status_note) VALUES (?, ?, ?, 'primary', ?, ?, ?)",
                    (i, key, f"Source {key}", status, "2022-05" if status != "active" else None, f"note {key}" if status != "active" else None))
    con.execute("INSERT INTO source_relations VALUES (6, 'replaces', 5), (5, 'replaces', 4), (10, 'replaces', 9)")
    return con


def cite(con, fid, *sids, status="active"):
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, status, source_key) VALUES (?, 1, ?, 'low', 'unreviewed', ?, ?)",
                (fid, f"fact {fid}", status, f"k{fid}"))
    for s in sids:
        con.execute("INSERT INTO fact_sources (fact_id, source_id) VALUES (?, ?)", (fid, s))


def test_audit_flags_each_status_and_not_the_clean_ones():
    con = db_with_sources()
    for i, sid in enumerate([1, 2, 3, 4, 7, 8], start=1):
        cite(con, i, sid)
    rows = ss.audit_source_status(con)
    got = {r["fact_id"]: r for r in rows}
    assert set(got) == {2, 3, 4}                                   # retracted, expression-of-concern, superseded with replacement
    assert got[2]["reason"] == "retracted" and got[2]["citekey"] == "ret" and got[2]["status_note"] == "note ret"
    assert got[2]["status_date"] == "2022-05" and got[2]["replacement"] == []
    assert got[3]["reason"] == "expression-of-concern"
    assert got[4]["reason"] == "superseded" and got[4]["derived"] is False


def test_replaces_chains_resolve_to_the_newest_source():
    con = db_with_sources()
    cite(con, 1, 4)                                                # cites the oldest edition
    cite(con, 2, 5)
    rows = {r["fact_id"]: r for r in ss.audit_source_status(con)}
    assert [x["citekey"] for x in rows[1]["replacement"]] == ["new"] and [x["citekey"] for x in rows[2]["replacement"]] == ["new"]
    assert rows[1]["replacement"][0]["name"] == "Source new"


def test_superseded_without_a_replacement_is_reported_separately_not_as_a_row():
    con = db_with_sources()
    cite(con, 1, 8)
    cite(con, 2, 8)
    assert ss.audit_source_status(con) == []
    assert ss.superseded_without_replacement(con) == [{"source_id": 8, "citekey": "orphan", "name": "Source orphan", "fact_ids": [1, 2]}]
    cite(con, 3, 4)
    assert ss.superseded_without_replacement(con)[0]["source_id"] == 8          # source 4 has a replacement: not an orphan


def test_a_replaces_relation_flags_a_source_whose_own_status_says_active():
    con = db_with_sources()
    cite(con, 1, 9)
    (row,) = ss.audit_source_status(con)
    assert (row["reason"], row["derived"], row["status"], [x["citekey"] for x in row["replacement"]]) == ("superseded", True, "active", ["quiet2"])
    cite(con, 2, 10)
    assert [r["fact_id"] for r in ss.audit_source_status(con)] == [1]           # the replacement itself is clean


def test_a_retracted_source_wins_over_a_replaces_relation():
    con = db_with_sources()
    con.execute("UPDATE sources SET status = 'retracted' WHERE id = 9")
    cite(con, 1, 9)
    (row,) = ss.audit_source_status(con)
    assert row["reason"] == "retracted" and row["replacement"] == []


def test_only_active_and_pending_facts_are_considered():
    con = db_with_sources()
    cite(con, 1, 2, status="retracted")
    cite(con, 2, 2, status="superseded")
    cite(con, 3, 2, status="pending")
    cite(con, 4, 2)
    assert [r["fact_id"] for r in ss.audit_source_status(con)] == [3, 4]


def test_multiple_sources_and_facts_one_row_each_ordered():
    con = db_with_sources()
    cite(con, 1, 2, 3, 1)                                          # one fact, two flagged sources, one clean
    cite(con, 2, 2)
    rows = ss.audit_source_status(con)
    assert [(r["fact_id"], r["source_id"]) for r in rows] == [(1, 2), (1, 3), (2, 2)]


def test_two_newest_replacements_are_both_reported():
    con = db_with_sources()
    con.execute("INSERT INTO sources (id, citekey, name, source_type) VALUES (11, 'alt', 'Alt', 'primary')")
    con.execute("INSERT INTO source_relations VALUES (11, 'replaces', 9)")
    cite(con, 1, 9)
    assert sorted(x["citekey"] for x in ss.audit_source_status(con)[0]["replacement"]) == ["alt", "quiet2"]


def test_no_citations_or_no_sources_is_empty_and_the_audit_never_writes():
    con = db_with_sources()
    assert ss.audit_source_status(con) == [] and ss.superseded_without_replacement(con) == []
    cite(con, 1, 2)
    before = con.total_changes
    ss.audit_source_status(con)
    ss.superseded_without_replacement(con)
    assert con.total_changes == before
    assert ss.audit_source_status(new_db()) == []


# ------------------------------------------------------------------ CLI

def cli_env(tmp_path):
    from test_cli_wiring import Env
    return Env(tmp_path)


def test_cli_audit_source_status_exit_codes_text_and_json(tmp_path):
    env = cli_env(tmp_path)
    r = env.cli("audit-source-status")
    assert (r.returncode, r.stdout, r.stderr) == (0, "No fact cites a retracted, doubtful or superseded source.\n", "")
    env.sql("UPDATE sources SET status = 'retracted', status_date = '2023-04', status_note = 'https://n.example/1' WHERE id = 1")
    r = env.cli("audit-source-status")
    assert r.returncode == 1 and "fact #1 cites smith2020: retracted since 2023-04" in r.stdout
    assert "https://n.example/1" in r.stdout and "Protein intake" in r.stdout
    j = json.loads(env.cli("audit-source-status", "--json").stdout)
    assert j["flagged_sources"][0]["reason"] == "retracted" and j["superseded_without_replacement"] == []
    env.sql("UPDATE sources SET status = 'superseded' WHERE id = 1")
    r = env.cli("audit-source-status")
    assert r.returncode == 0 and "nothing replaces them" in r.stderr
    env.sql("INSERT INTO sources (id, citekey, name, source_type) VALUES (9, 'smith2024', 'Smith 2024', 'primary')")
    env.sql("INSERT INTO source_relations VALUES (9, 'replaces', 1)")
    r = env.cli("audit-source-status")
    assert r.returncode == 1 and "superseded" in r.stdout and "by smith2024" in r.stdout


# ------------------------------------------------------------------ normal-only DB

def test_normal_db_carries_status_and_edition_but_not_the_note_date_or_relations():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        con = sqlite3.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
        assert {"status", "edition"} <= cols and not ({"status_note", "status_date", "original_published_date", "origin_path"} & cols)
        assert tuple(con.execute("SELECT status, edition FROM sources WHERE citekey = 'pub2020'").fetchone()) == ("corrected", "2nd edition")
        assert "source_relations" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        assert leak_test.MARKERS["source status note"].encode() not in open(path, "rb").read()
        assert not leak_test.scan_against_full(path, full)


def test_normal_db_drops_an_edition_that_trips_a_keyword():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.execute("UPDATE sources SET edition = 'Zorbak family edition' WHERE id = 1")
        c.commit()
        c.close()
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules(keywords=("zorbak",)))
        con = sqlite3.connect(path)
        assert tuple(con.execute("SELECT status, edition FROM sources WHERE id = 1").fetchone()) == ("corrected", None)


def test_leak_test_flags_a_status_note_planted_in_the_normal_db():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        con = sqlite3.connect(path)
        con.execute("UPDATE sources SET edition = ? WHERE id = 1", (leak_test.MARKERS["source status note"],))
        con.commit()
        con.close()
        assert leak_test.scan_against_full(path, full)
