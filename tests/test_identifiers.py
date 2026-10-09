"""#48: persistent identifiers for sources. Normalization and checksums, loading from frontmatter and
manual_sources.json, duplicate detection (reported, never merged), `source ids`, `--identifier`, and the
normal-only DB. Temp dirs only; no network."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

import pytest

import identifiers as ids
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


# ------------------------------------------------------------------ normalization

@pytest.mark.parametrize("raw", ["10.1210/JC.2007-1692", "https://doi.org/10.1210/jc.2007-1692", "http://dx.doi.org/10.1210/jc.2007-1692",
                                 "doi:10.1210/jc.2007-1692", "DOI: 10.1210/jc.2007-1692", "  10.1210/jc.2007-1692.  ", "10.1210/jc.2007-1692),",
                                 "HTTPS://DOI.ORG/10.1210/JC.2007-1692"])
def test_doi_forms_normalize_to_one_value(raw):
    assert ids.normalize("doi", raw) == "10.1210/jc.2007-1692"


@pytest.mark.parametrize("raw", ["", "10.12/short-registrant", "11.1210/x", "doi", "10.1210/", "10.1210", "https://example.org/10.1210/x", 5, None])
def test_bad_dois_are_rejected_naming_the_value(raw):
    with pytest.raises(ids.IdentifierError) as e:
        ids.normalize("doi", raw)
    assert "doi" in str(e.value) and repr(raw) in str(e.value)


def test_isbn_10_and_13_are_the_same_book_and_hyphens_do_not_matter():
    assert ids.normalize("isbn", "978-1-4925-9767-4") == "9781492597674"
    assert ids.normalize("isbn", "ISBN 978 1 4925 9767 4") == "9781492597674"
    assert ids.normalize("isbn", "0-306-40615-2") == ids.normalize("isbn", "9780306406157") == "9780306406157"
    x13 = ids.normalize("isbn", "0-8044-2957-X")                              # an ISBN-10 whose check digit is X
    assert len(x13) == 13 and x13.startswith("978") and ids.normalize("isbn", x13) == x13 == ids.normalize("isbn", "080442957x")
    assert ids.normalize("isbn", "isbn-10: 0-306-40615-2") == "9780306406157"


@pytest.mark.parametrize("raw, why", [("0-306-40615-3", "ISBN-10 check digit"), ("978-0-306-40615-8", "ISBN-13 check digit"),
                                      ("9770306406157", "starts with 978 or 979"), ("12345", "10 or 13 digits"), ("", "10 or 13 digits"),
                                      ("978030640615X", "10 or 13 digits"), ("0-306-4061-52X", "10 or 13 digits")])
def test_bad_isbns_name_the_offending_value(raw, why):
    with pytest.raises(ids.IdentifierError, match=why) as e:
        ids.normalize("isbn", raw)
    assert repr(raw) in str(e.value)


def test_issn_pmid_arxiv_other():
    assert ids.normalize("issn", "0378-5955") == "0378-5955" and ids.normalize("issn", "03785955") == "0378-5955"
    assert ids.normalize("issn", "ISSN 2434-561x") == "2434-561X"
    for bad in ("0378-5954", "1234", "abcd-efgh"):
        with pytest.raises(ids.IdentifierError):
            ids.normalize("issn", bad)
    assert ids.normalize("pmid", "19135656") == "19135656" and ids.normalize("pmid", "PMID: 8563679") == "8563679"
    for bad in ("0123", "12a", "", "1234567890"):
        with pytest.raises(ids.IdentifierError):
            ids.normalize("pmid", bad)
    assert ids.normalize("arxiv", "arXiv:2101.00001v3") == "2101.00001"
    assert ids.normalize("arxiv", "https://arxiv.org/abs/2101.00001v2") == "2101.00001"
    assert ids.normalize("arxiv", "https://arxiv.org/pdf/hep-th/9901001v1.pdf") == "hep-th/9901001"
    with pytest.raises(ids.IdentifierError):
        ids.normalize("arxiv", "21.1")
    assert ids.normalize("other", "  some   registry  id ") == "some registry id"
    with pytest.raises(ids.IdentifierError):
        ids.normalize("other", "   ")
    assert ids.pmcid("pmc4721027") == "pmcid:PMC4721027" and ids.pmcid("PMCID: PMC12267013") == "pmcid:PMC12267013"
    with pytest.raises(ids.IdentifierError):
        ids.pmcid("4721027")
    with pytest.raises(ids.IdentifierError, match="scheme"):
        ids.normalize("orcid", "0000")


def test_candidates_for_an_unknown_scheme():
    assert ("doi", "10.1210/jc.2007-1692") in ids.candidates("https://doi.org/10.1210/JC.2007-1692")
    assert ids.candidates("doi:10.1210/x1234") == [("doi", "10.1210/x1234")]
    assert ("isbn", "9780306406157") in ids.candidates("0-306-40615-2")
    assert ("pmid", "19135656") in ids.candidates("19135656")
    assert ids.candidates("PMC4721027") == [("other", "pmcid:PMC4721027"), ("other", "PMC4721027")]
    assert ids.candidates("pmcid:PMC4721027") == [("other", "pmcid:PMC4721027")]
    assert ids.candidates("isbn:1234") == [] and ids.candidates("") == [] and ids.candidates(None) == []


def test_filter_clause_uses_bound_parameters():
    sql, params = ids.filter_clause("x' OR '1'='1; DROP TABLE facts")
    assert "DROP" not in sql and "x' OR" not in sql and any("drop table facts" in str(p).lower() for p in params)
    with pytest.raises(ValueError):
        ids.filter_clause("   ")


# ------------------------------------------------------------------ loading

def load(name, tag="m"):
    spec = importlib.util.spec_from_file_location(f"{tag}_{name}", os.path.join(REPO, "ingest" if os.path.exists(os.path.join(REPO, "ingest", name)) else "", name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_vault(tmp_path, notes):
    d = tmp_path / "vault" / "sources"
    d.mkdir(parents=True)
    for name, fm in notes.items():
        body = "\n".join(f"{k}: {v}" for k, v in {"title": name, "year": "2020", "source-type": "peer-reviewed-study", **fm}.items())
        (d / f"{name}.md").write_text(f"---\n{body}\n---\nbody\n")
    return str(d)


def run02(ingest, tmp_path, notes):
    mod = load("literature_sources.py")
    mod.SRC_DIR = write_vault(tmp_path, notes)
    con = ingest.db()
    mod.run(con)
    return con


def table(con):
    return sorted(tuple(r) for r in con.execute("SELECT s.citekey, i.scheme, i.value FROM source_identifiers i JOIN sources s ON s.id = i.source_id"))


def test_02_pin_counts_then_identifiers_from_the_fields_the_vault_really_has(ingest, tmp_path):
    con = run02(ingest, tmp_path, {"a2020": {"doi": "10.1210/JC.2007-1692", "pmid": "19135656", "pmcid": "PMC4721027"},
                                    "b2021": {"isbn": "978-1-4925-9767-4"}, "plain2022": {}})
    assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 3                  # unchanged by the new table
    assert table(con) == [("a2020", "doi", "10.1210/jc.2007-1692"), ("a2020", "other", "pmcid:PMC4721027"), ("a2020", "pmid", "19135656"),
                          ("b2021", "isbn", "9781492597674")]                                # a source with several; one with none
    assert con.execute("SELECT COUNT(*) FROM source_identifier_conflicts").fetchone()[0] == 0


@pytest.mark.parametrize("fm, msg", [({"doi": "not-a-doi"}, "doi 'not-a-doi'"), ({"isbn": "978-0-306-40615-8"}, "ISBN-13 check digit"),
                                      ({"pmid": "0123"}, "pmid '0123'"), ({"pmcid": "1234"}, "PMC id")])
def test_02_a_bad_identifier_fails_the_build_naming_note_and_value(ingest, tmp_path, fm, msg):
    with pytest.raises(Exception, match=msg) as e:
        run02(ingest, tmp_path, {"bad2020": fm})
    assert "bad2020" in str(e.value) and type(e.value).__name__ == "IdentifierError"


def test_placeholders_mean_no_identifier_but_other_junk_still_fails(ingest, tmp_path):
    con = run02(ingest, tmp_path, {"a2020": {"doi": "n/a", "pmid": "None", "isbn": "-", "pmcid": "TBD"}, "b2021": {"doi": "10.1210/real1"}})
    assert table(con) == [("b2021", "doi", "10.1210/real1")]
    assert all(ids.is_placeholder(p) for p in ("N/A", " na ", "Unknown", "?")) and not ids.is_placeholder("10.1210/x") and not ids.is_placeholder(None)
    with pytest.raises(Exception, match="doi 'see paper'"):
        run02(ingest, tmp_path / "again", {"c2022": {"doi": "see paper"}})


def test_duplicates_are_reported_with_both_citekeys_and_not_merged(ingest, tmp_path, capsys):
    con = run02(ingest, tmp_path, {"first2020": {"doi": "10.1210/jc.2007-1692"}, "second2021": {"doi": "https://doi.org/10.1210/JC.2007-1692", "pmid": "19135656"}})
    out = capsys.readouterr().out
    assert "duplicate identifier doi:10.1210/jc.2007-1692" in out and "'first2020'" in out and "'second2021'" in out
    assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2                    # both sources kept
    assert table(con) == [("first2020", "doi", "10.1210/jc.2007-1692"), ("second2021", "pmid", "19135656")]
    (c,) = con.execute("SELECT s.citekey, c.scheme, c.value, o.citekey FROM source_identifier_conflicts c JOIN sources s ON s.id = c.source_id "
                       "JOIN sources o ON o.id = c.owner_source_id").fetchall()
    assert tuple(c) == ("second2021", "doi", "10.1210/jc.2007-1692", "first2020")


def test_the_same_identifier_twice_on_one_source_is_not_a_conflict(ingest, tmp_path, capsys):
    con = run02(ingest, tmp_path, {"a2020": {"doi": "10.1210/x1234", "pmid": "123"}})
    from ingest import shared
    cur = con.cursor()
    sid = con.execute("SELECT id FROM sources").fetchone()[0]
    assert shared.add_source_identifiers(cur, sid, "a2020", [("doi", "10.1210/x1234")]) == []
    assert "duplicate" not in capsys.readouterr().out and con.execute("SELECT COUNT(*) FROM source_identifiers").fetchone()[0] == 2


def test_manual_sources_take_identifiers_as_fields_or_a_mapping(ingest):
    with open(os.path.join(ingest.env.data_dir, "manual_sources.json"), "w") as f:
        json.dump([{"citekey": "m1", "name": "M", "source_type": "primary", "doi": "10.5555/ABC123"},
                   {"citekey": "m2", "name": "M2", "source_type": "primary", "identifiers": {"isbn": "0-306-40615-2", "other": ["reg-77", "reg-78"]}},
                   {"citekey": "m3", "name": "M3", "source_type": "primary"}], f)
    con = ingest.db()
    load("seed_sources.py").run(con)
    assert ("m1", "doi", "10.5555/abc123") in table(con) and ("m2", "isbn", "9780306406157") in table(con)
    assert con.execute("SELECT COUNT(*) FROM source_identifiers WHERE source_id = (SELECT id FROM sources WHERE citekey = 'm3')").fetchone()[0] == 0


def test_schema_constraints():
    con = sqlite3.connect(":memory:")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO sources (id, name, source_type) VALUES (1, 'A', 'primary'), (2, 'B', 'primary')")
    con.execute("INSERT INTO source_identifiers VALUES (1, 'doi', '10.1/x')")
    for sql in ("INSERT INTO source_identifiers VALUES (2, 'doi', '10.1/x')", "INSERT INTO source_identifiers VALUES (1, 'orcid', 'z')",
                "INSERT INTO source_identifiers VALUES (1, 'doi', '  ')"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sql)
    con.execute("INSERT INTO source_identifiers VALUES (2, 'pmid', '10.1/x')")           # same value, different scheme: fine
    assert set(ids.SCHEMES) == {"doi", "isbn", "issn", "pmid", "arxiv", "other"}


# ------------------------------------------------------------------ queries and CLI

def _qdb():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (id, name) VALUES (1, 's')")
    con.execute("INSERT INTO sources (id, citekey, name, source_type) VALUES (1, 'a', 'A', 'primary'), (2, 'b', 'B', 'primary')")
    con.executemany("INSERT INTO source_identifiers VALUES (?, ?, ?)", [(1, "doi", "10.1210/jc.2007-1692"), (1, "pmid", "19135656"), (2, "isbn", "9780306406157")])
    for i, (vis, src) in enumerate([("normal", 1), ("normal", 2), ("private", 1)], start=1):
        con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, freshness, visibility, source_key) VALUES (?, 1, ?, 'low', 'unreviewed', ?, ?)",
                    (i, f"banana {i}", vis, f"k{i}"))
        con.execute("INSERT INTO fact_sources (fact_id, source_id) VALUES (?, ?)", (i, src))
    return con


def test_identifier_filter_matches_any_spelling_in_both_layers():
    import knowledge
    import modes
    import modes_store
    con = _qdb()
    for ref in ("10.1210/JC.2007-1692", "https://doi.org/10.1210/jc.2007-1692", "doi:10.1210/jc.2007-1692", "19135656", "PMID 19135656"):
        assert [r["id"] for r in knowledge.list_facts(con, identifier=ref)] == [1, 3], ref
    assert [r["id"] for r in knowledge.list_facts(con, identifier="0-306-40615-2")] == [2]
    assert [r["id"] for r in knowledge.search_facts(con, "banana", identifier="9780306406157")] == [2]
    assert knowledge.list_facts(con, identifier="10.9999/nobody") == []
    with pytest.raises(ValueError):
        knowledge.list_facts(con, identifier="  ")
    normal, private = modes.Session(mode=modes.Mode.normal), modes.Session(mode=modes.Mode.private)
    assert [r["id"] for r in modes_store.list_facts(normal, con, identifier="19135656")] == [1]
    assert [r["id"] for r in modes_store.list_facts(private, con, identifier="19135656")] == [1, 3]
    with pytest.raises(ValueError):
        modes_store.list_facts(normal, con, identifier="")


def cli_env(tmp_path):
    from test_cli_wiring import Env
    return Env(tmp_path)


def test_cli_source_ids_and_search_identifier(tmp_path):
    env = cli_env(tmp_path)
    env.sql("INSERT INTO source_identifiers VALUES (1, 'doi', '10.1210/x1'), (1, 'pmid', '555')")
    env.sql("INSERT INTO source_identifiers VALUES (2, 'isbn', '9780306406157')")
    env.sql("INSERT INTO source_identifier_conflicts VALUES (2, 'doi', '10.1210/x1', 1)")
    out = env.cli("source", "ids", "smith2020").stdout
    assert "smith2020" in out and "doi: 10.1210/x1" in out and "pmid: 555" in out
    j = json.loads(env.cli("source", "ids", "blog1", "--json").stdout)
    assert j["identifiers"] == [{"scheme": "isbn", "value": "9780306406157"}] and j["conflicts"] == [{"scheme": "doi", "value": "10.1210/x1", "owner": "smith2020"}]
    assert "shared, not recorded here: doi:10.1210/x1 belongs to smith2020" in env.cli("source", "ids", "blog1").stdout
    r = env.cli("source", "ids", "ghost")
    assert r.returncode == 1 and "unknown source" in r.stderr
    rows = json.loads(env.cli("search", "protein", "--identifier", "https://doi.org/10.1210/X1", "--json").stdout)
    assert [x["id"] for x in rows] == [1]
    assert [x["id"] for x in json.loads(env.cli("facts", "--identifier", "555", "--json").stdout)] == [1]
    assert env.cli("facts", "--identifier", "   ").returncode == 1


# ------------------------------------------------------------------ normal-only DB

def test_normal_db_carries_identifiers_of_included_sources_only():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        c = sqlite3.connect(full)
        c.executemany("INSERT INTO source_identifiers VALUES (?, ?, ?)", [(1, "doi", "10.1210/pub"), (2, "doi", "10.1210/onlyprivate"), (1, "other", "pmcid:PMC1234567")])
        c.execute("INSERT INTO source_identifier_conflicts VALUES (2, 'doi', '10.1210/pub', 1)")
        c.commit()
        c.close()
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules(keywords=("pmc1234567",)))
        con = sqlite3.connect(path)
        assert [tuple(r) for r in con.execute("SELECT * FROM source_identifiers")] == [(1, "doi", "10.1210/pub")]       # source 2 not included; the keyword drops the 'other'
        assert "source_identifier_conflicts" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
