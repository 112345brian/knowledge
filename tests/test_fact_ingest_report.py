"""What the two fact steps print and which rows/links they write, pinned as a whole.

Both steps now share one implementation of the ingest decisions (fact_ingest_service); this file pins the
observable result of that implementation for a mixed input: accepted facts, silent and warned skips, a
citekey the sources lack, entries that get a derived key, and the lines the steps print.
"""
import json
import os

from test_fact_ingest import F, ingest  # noqa: F401  (fixture)

SHA = "a" * 64


def _mixed():
    return [
        F(statement="Kept one.", source_key="k-one", trust_level="high", recheck_by="2027-01-01", freshness="recheck"),
        F(statement="Derived key.", freshness="no-decay", recheck_rationale="stable"),
        F(statement="Bad visibility.", visibility="public"),
        F(statement="Bad status.", status="gone"),
        F(statement="", subject="s"),
        F(statement="Unknown trust.", trust_level="bogus"),
        F(statement="Cites nothing known.", source_citekey="no-such-citekey", source_locator="p. 1"),
        F(statement="With baseline.", extracted_from_sha256=SHA),
    ]


def _sources(con):
    con.execute("INSERT INTO sources (citekey, name, source_type) VALUES ('known', 'A known source', 'primary')")
    con.commit()


def test_fact_files_step_prints_the_pilot_report(ingest, capsys):
    con = ingest.db()
    _sources(con)
    items = _mixed()
    items[1].pop("source_key", None)
    items.append(F(statement="Cites a known one.", source_citekey="known", source_locator="ch. 2", source_quote="q"))
    ingest.run04(items, con)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("  WARNING -- skipping fact with invalid visibility 'public'")
    assert out[1].startswith("  WARNING -- skipping fact with invalid status 'gone'")
    assert out[2:] == [
        "[facts] inserted 5, skipped 4 (bad shape)",
        "  WARNING -- citekeys referenced but not found in sources: ['no-such-citekey']",
        "  note: 4 entries have no source_key yet; used deterministic legacy-* keys. "
        "Run backfill_source_keys.py to write them into the files (same values).",
        "  facts by freshness: {'no-decay': 1, 'recheck': 1, 'unreviewed': 3}",
        "  facts by trust_level: {'high': 1, 'low': 4}",
        "  facts by is_personal: {0: 5}",
    ], out
    assert con.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 5


def test_general_step_prints_the_general_report(ingest, capsys):
    con = ingest.db()
    _sources(con)
    items = [dict(i, freshness="no-decay", recheck_rationale="r") for i in _mixed()]
    items.append(F(statement="Cites a known one.", source_citekey="known", freshness="no-decay", recheck_rationale="r"))
    ingest.run11(items, con)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("  WARNING -- skipping fact with invalid visibility 'public'")
    assert out[1].startswith("  WARNING -- skipping fact with invalid status 'gone'")
    assert out[2:] == [
        "[seed_general_facts] inserted 5, skipped 4 (bad shape)",
        "  WARNING -- citekeys referenced but not found in sources: ['no-such-citekey']",
        "  note: 4 entries have no source_key yet; used deterministic legacy-* keys. "
        "Run backfill_source_keys.py to write them into the files (same values).",
    ], out
    assert con.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 1


def test_the_privacy_floor_is_applied_after_the_insert_and_reported(ingest, capsys):
    rules = {"version": 1, "keywords": ["secretword"]}
    rules_path = os.path.join(ingest.env.data_dir, "privacy_rules.json")
    with open(rules_path, "w") as f:
        json.dump(rules, f)
    con = ingest.run04([F(statement="Mentions secretword here.", visibility="normal"), F(statement="Plain.", visibility="normal")])
    got = {r[0]: r[1] for r in con.execute("SELECT statement, visibility FROM facts")}
    assert got == {"Mentions secretword here.": "private", "Plain.": "normal"}
    assert "  privacy rules raised 1 fact(s) to private" in capsys.readouterr().out
