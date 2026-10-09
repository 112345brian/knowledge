"""#1 part 2: claims.inference_type + stale-premise audit (part 1, the pinned old behaviour, is
test_claims_audit.py). Superseded/retracted facts are written straight into a fixture db built from
the real schema.sql, since nothing in the pipeline can set that status yet (#8)."""
import glob
import os
import sqlite3
from datetime import date

import pytest

import claims_store as ca
import clock
from conftest import REPO
from test_claims_audit import add_claim, add_fact, load_seed, new_db

TODAY = "2026-10-03"


def test_inference_type_is_nullable_checked_and_not_backfilled():
    con = new_db()
    c = add_claim(con, "plain")
    assert con.execute("SELECT inference_type FROM claims WHERE id=?", (c,)).fetchone()[0] is None
    for ok in ("deductive", "inductive", "abductive"):
        add_claim(con, ok, inference_type=ok)
    for bad in ("analogical", "", "Deductive"):
        with pytest.raises(sqlite3.IntegrityError):
            add_claim(con, "bad", inference_type=bad)


def test_seed_claims_leave_inference_type_null():
    con = new_db()
    load_seed().run(con)
    assert con.execute("SELECT COUNT(*) FROM claims WHERE inference_type IS NOT NULL").fetchone()[0] == 0


def test_seed_claims_accepts_optional_inference_type(monkeypatch):
    con = new_db()
    seed = load_seed()
    monkeypatch.setattr(seed, "CLAIMS", [dict(statement="s", notes="n", fact_match_prefixes=[], inference_type="inductive")])
    seed.run(con)
    assert con.execute("SELECT inference_type FROM claims").fetchone()[0] == "inductive"


def test_empty_db_and_claim_without_facts_yield_nothing():
    con = new_db()
    assert ca.audit_claims(con, TODAY) == []
    add_claim(con, "lonely")
    assert ca.audit_claims(con, TODAY) == []


def test_all_active_current_facts_yield_nothing():
    con = new_db()
    add_claim(con, "c", [add_fact(con, "a"), add_fact(con, "b", recheck_by="2027-01-01")])
    assert ca.audit_claims(con, TODAY) == []


@pytest.mark.parametrize("status", ["superseded", "retracted"])
def test_superseded_and_retracted_premises_are_flagged_with_context(status):
    con = new_db()
    new = add_fact(con, "new")
    old = add_fact(con, "old", status=status, trust_rationale="why", notes="n",
                   superseded_by=new if status == "superseded" else None)
    ok = add_fact(con, "fine")
    c = add_claim(con, "conclusion", [old, ok], inference_type="deductive")
    (row,) = ca.audit_claims(con, TODAY)
    assert row["claim_id"] == c and row["fact_id"] == old and row["reason"] == status
    assert row["inference_type"] == "deductive" and row["trust_rationale"] == "why" and row["notes"] == "n"
    assert row["superseded_by_fact_id"] == (new if status == "superseded" else None)
    (v,) = con.execute("SELECT claim_id, fact_id, reason FROM v_claims_with_stale_premises").fetchall()
    assert v == (c, old, status)


def test_fact_cited_by_several_claims_gives_a_row_per_claim():
    con = new_db()
    f = add_fact(con, "shared", status="retracted")
    c1, c2 = add_claim(con, "one", [f]), add_claim(con, "two", [f])
    assert [(r["claim_id"], r["fact_id"]) for r in ca.audit_claims(con, TODAY)] == [(c1, f), (c2, f)]


def test_claim_with_two_stale_premises_gives_two_rows_with_own_reasons():
    con = new_db()
    a = add_fact(con, "a", status="superseded")
    b = add_fact(con, "b", recheck_by="2026-01-01")
    add_claim(con, "c", [a, b])
    assert [(r["fact_id"], r["reason"]) for r in ca.audit_claims(con, TODAY)] == [(a, "superseded"), (b, "past_recheck_by")]


def test_inference_claim_used_as_premise_does_not_cascade():
    # claim-citing-claim is out of scope: claims are never premises, so a stale inner claim does not
    # flag an outer claim that happens to share nothing with it
    con = new_db()
    add_claim(con, "inner", [add_fact(con, "premise", status="retracted")], inference_type="inductive")
    add_claim(con, "outer", [add_fact(con, "other")])
    assert [r["claim_statement"] for r in ca.audit_claims(con, TODAY)] == ["inner"]


@pytest.mark.parametrize("value,flagged", [
    ("2026-10-02", True), ("2026-10-03", False), ("2026-10-04", False),
    ("2026-09", True), ("2026-10", False), ("2025", True), ("2026", False),
    ("2026-10-02T09:00:00+00:00", True),
    (None, False), ("", False), ("   ", False),
    ("next panel", False), ("after surgery", False), ("2026-13-01", False), ("2026-02-30", False),
    ("0000-01-01", False), ("10/02/2026", False), ("2026-10-02 maybe", True),
])
def test_recheck_by_parsing_and_boundary(value, flagged):
    con = new_db()
    add_claim(con, "c", [add_fact(con, "f", recheck_by=value)])
    rows = ca.audit_claims(con, TODAY)
    assert bool(rows) == flagged
    if flagged:
        assert rows[0]["reason"] == "past_recheck_by"


def test_superseded_wins_over_past_recheck_by_and_is_one_row():
    con = new_db()
    add_claim(con, "c", [add_fact(con, "f", status="superseded", recheck_by="2020-01-01")])
    assert [r["reason"] for r in ca.audit_claims(con, TODAY)] == ["superseded"]


def test_today_defaults_to_the_clock_helper_and_accepts_a_date_object():
    con = new_db()
    add_claim(con, "c", [add_fact(con, "f", recheck_by="2026-10-03")])
    with clock.frozen("2026-10-03T08:00:00+00:00"):
        assert ca.audit_claims(con) == []
    with clock.frozen("2026-10-04T08:00:00+00:00"):
        assert len(ca.audit_claims(con)) == 1
    assert len(ca.audit_claims(con, date(2026, 10, 4))) == 1


def test_unparseable_rechecks_are_reported_not_flagged():
    con = new_db()
    a = add_fact(con, "a", recheck_by="next panel")
    add_claim(con, "c", [a, add_fact(con, "b", recheck_by="2030-01-01"), add_fact(con, "c", recheck_by=None)])
    add_fact(con, "uncited", recheck_by="whenever")
    assert ca.unparseable_rechecks(con) == [{"fact_id": a, "recheck_by": "next panel"}]


def test_audit_is_read_only_and_restores_row_factory():
    con = new_db()
    add_claim(con, "c", [add_fact(con, "f", status="retracted")])
    before = con.total_changes
    ca.audit_claims(con, TODAY)
    assert con.total_changes == before and con.row_factory is None


def test_flipping_a_seeded_claims_fact_to_superseded_surfaces_it_then_reverts():
    # the issue's manual check, on a throwaway db
    con = new_db()
    seed = load_seed()
    for p in seed.CLAIMS[1]["fact_match_prefixes"]:
        add_fact(con, p + " x")
    seed.run(con)
    assert ca.audit_claims(con, TODAY) == []
    con.execute("UPDATE facts SET status='superseded'")
    assert {r["claim_id"] for r in ca.audit_claims(con, TODAY)} == {2}
    con.execute("UPDATE facts SET status='active'")
    assert ca.audit_claims(con, TODAY) == []


def test_no_other_module_reads_claim_facts():
    # the audit result cannot be silently discarded by a caller that does not exist yet; when the CLI
    # lands it must use audit_claims, so this scan lists the only allowed readers
    allowed = {"claims_audit.py", "claims_store.py", "seed_claims.py", "build.py", "build_rules.py"}  # build* only count rows
    for path in glob.glob(os.path.join(REPO, "*.py")):
        name = os.path.basename(path)
        if name not in allowed:
            assert "claim_facts" not in open(path).read(), name
