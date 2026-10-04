"""#24: provenance fields on facts (captured_via, session_id, captured_at, source_quote).

Not covered here: `knowledge.py show` printing them (knowledge.py is being refactored elsewhere).
"""
import json
import os
import re
import sqlite3

import pytest

import clock
from test_add_fact import Env
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)

FROZEN = "2026-10-03T08:00:00+00:00"
ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def af(ingest):  # noqa: F811  (add_fact imported against the throwaway private dir)
    import add_fact
    return add_fact


def nf(af, **kw):
    base = dict(statement="A fact.", subject="x", trust_level="low", no_decay=True, recheck_rationale="no decay")
    base.update(kw)
    return af.NewFact(**base)


MCP = dict(captured_via="mcp", session_id="sess-123", source_quote="I said so, in my own words.")


# ---------------------------------------------------------------- pins (true before the change)

def _con_with_source(ingest):
    con = ingest.db()
    con.execute("INSERT INTO sources (citekey, name, source_type) VALUES ('ck', 'Src', 'primary')")
    return con


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_source_quote_with_a_citekey_still_lands_in_fact_sources(ingest, run):
    con = getattr(ingest, run)([F(source_citekey="ck", source_locator="p. 1", source_quote="the quote")], con=_con_with_source(ingest))
    r = con.execute("SELECT locator, quote FROM fact_sources").fetchone()
    assert (r["locator"], r["quote"]) == ("p. 1", "the quote")


def test_cli_without_provenance_writes_no_provenance_keys(env):
    assert env.cli("S.", "--subject", "x", "--trust", "low", "--no-decay", "--recheck-rationale", "no decay").returncode == 0
    assert not {"captured_via", "session_id", "captured_at"} & set(env.entries()[0])


def test_cli_still_rejects_a_quote_without_a_citekey(env):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--no-decay", "--recheck-rationale", "no decay", "--source-quote", "q")
    assert r.returncode == 1 and "without --source-citekey" in r.stderr


# ---------------------------------------------------------------- schema + ingest

def test_provenance_columns_exist_and_are_nullable(ingest):
    con = ingest.db()
    con.execute("INSERT INTO subjects (name) VALUES ('s')")
    con.execute("INSERT INTO facts (subject_id, statement, trust_level, freshness) VALUES (1, 'x', 'low', 'unreviewed')")
    r = con.execute("SELECT captured_via, session_id, captured_at, source_quote FROM facts").fetchone()
    assert tuple(r) == (None, None, None, None)


@pytest.mark.parametrize("run", ["run04", "run11"])
def test_ingest_stores_provenance_and_leaves_it_null_when_absent(ingest, run):
    con = getattr(ingest, run)([
        F(statement="With.", freshness="no-decay", recheck_rationale="no decay", captured_via="mcp", session_id="s1", captured_at=FROZEN, source_quote="my words"),
        F(statement="Without."),
    ])
    rows = {r["statement"]: tuple(r)[1:] for r in
            con.execute("SELECT statement, captured_via, session_id, captured_at, source_quote FROM facts")}
    assert rows["With."] == ("mcp", "s1", FROZEN, "my words")
    assert rows["Without."] == (None, None, None, None)


def test_ingest_stores_a_quote_that_has_no_citekey(ingest):
    con = ingest.run11([F(captured_via="mcp", session_id="s", source_quote="solo quote")])
    assert con.execute("SELECT source_quote FROM facts").fetchone()[0] == "solo quote"
    assert con.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 0


# ---------------------------------------------------------------- library validation

def test_mcp_without_a_quote_is_rejected(af, tmp_path):
    p = tmp_path / "f.json"
    res = af.append_fact(nf(af, captured_via="mcp", session_id="s"), data_path=str(p), db_path=str(tmp_path / "n.db"))
    assert not res.ok and any("source_quote" in e for e in res.errors) and not p.exists()


@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t"])
def test_mcp_blank_quote_or_session_is_rejected(af, blank):
    for field in ("source_quote", "session_id"):
        kw = dict(MCP, **{field: blank})
        errors, _ = af.validate_fact(nf(af, **kw))
        assert any(field in e for e in errors), (field, blank)


def test_mcp_with_full_provenance_is_accepted_and_stamped(af, tmp_path):
    p = tmp_path / "f.json"
    with clock.frozen(FROZEN):
        res = af.append_fact(nf(af, **MCP), data_path=str(p), db_path=str(tmp_path / "n.db"))
    assert res.ok, res.errors
    e = json.loads(p.read_text())[0]
    assert (e["captured_via"], e["session_id"], e["captured_at"], e["source_quote"]) == \
        ("mcp", "sess-123", FROZEN, "I said so, in my own words.")
    assert "source_citekey" not in e  # a quote needs no citekey once there is provenance


def test_captured_at_from_the_caller_is_kept_but_must_be_a_utc_offset_timestamp(af):
    ok, _ = af.validate_fact(nf(af, **MCP, captured_at="2026-10-03T08:00:00+00:00"))
    assert ok == []
    for bad in ("2026-10-03", "2026-10-03T08:00:00", "yesterday", 5):
        errors, _ = af.validate_fact(nf(af, **MCP, captured_at=bad))
        assert any("captured_at" in e for e in errors), bad


def test_other_sources_do_not_require_provenance_fields(af):
    for via in ("cli", "migrate-memory", "some-future-thing"):
        errors, _ = af.validate_fact(nf(af, captured_via=via))
        assert errors == [], via


@pytest.mark.parametrize("bad", ["", "  ", "MCP", "has space", 7, ["mcp"]])
def test_captured_via_must_be_a_kebab_token_when_given(af, bad):
    errors, _ = af.validate_fact(nf(af, captured_via=bad))
    assert any("captured_via" in e for e in errors), bad


def test_a_quote_without_citekey_is_only_allowed_with_a_captured_via(af):
    errors, _ = af.validate_fact(nf(af, source_quote="q"))
    assert any("without --source-citekey" in e for e in errors)
    errors, _ = af.validate_fact(nf(af, source_quote="q", captured_via="migrate-memory"))
    assert errors == []
    errors, _ = af.validate_fact(nf(af, source_locator="p. 1", captured_via="mcp", session_id="s", source_quote="q"))
    assert any("without --source-citekey" in e for e in errors)  # a locator still needs a citekey


def test_provenance_key_order_is_after_the_existing_keys(af):
    with clock.frozen(FROZEN):
        e = af.build_entry(nf(af, **MCP))
    assert list(e)[-4:] == ["source_quote", "captured_via", "session_id", "captured_at"]


def test_nul_in_quote_is_pinned_as_accepted(af):
    errors, _ = af.validate_fact(nf(af, **dict(MCP, source_quote="a\x00b")))
    assert errors == []


# ---------------------------------------------------------------- CLI

def test_cli_flags_record_provenance(env):
    env.env["KNOWLEDGE_FROZEN_NOW"] = FROZEN
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--no-decay", "--recheck-rationale", "no decay", "--captured-via", "mcp", "--session-id", "s9",
                "--source-quote", "my words")
    assert r.returncode == 0, r.stderr
    e = env.entries()[0]
    assert (e["captured_via"], e["session_id"], e["captured_at"], e["source_quote"]) == ("mcp", "s9", FROZEN, "my words")


def test_cli_mcp_without_session_or_quote_is_rejected_and_writes_nothing(env):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--no-decay", "--recheck-rationale", "no decay", "--captured-via", "mcp")
    assert r.returncode == 1 and "session_id" in r.stderr and "source_quote" in r.stderr
    assert not os.path.exists(env.facts)


def test_full_round_trip_into_the_db(ingest, af):
    path = os.path.join(ingest.env.data_dir, "general_facts.json")
    with clock.frozen(FROZEN):
        assert af.append_fact(nf(af, **MCP), data_path=path, db_path=os.path.join(ingest.env.root, "n.db")).ok
    ingest.mod11.DATA_DIR = ingest.env.data_dir
    con = ingest.db()
    ingest.mod11.run(con)
    r = con.execute("SELECT captured_via, session_id, captured_at, source_quote, visibility FROM facts").fetchone()
    assert tuple(r) == ("mcp", "sess-123", FROZEN, "I said so, in my own words.", "private")


def test_session_id_or_captured_at_without_captured_via_is_rejected_not_silently_dropped(af, env):
    for kw in (dict(session_id="s"), dict(captured_at=FROZEN)):
        errors, _ = af.validate_fact(nf(af, **kw))
        assert any("without captured_via" in e for e in errors), kw
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--no-decay", "--recheck-rationale", "no decay", "--session-id", "s")
    assert r.returncode == 1 and not os.path.exists(env.facts)
