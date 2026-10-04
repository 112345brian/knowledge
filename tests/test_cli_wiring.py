"""CLI wiring (#30, #31, #1, #24): history, show --as-of, audit-claims, privacy *.

Library behavior is covered elsewhere (test_fact_revisions, test_privacy, test_claims_audit); this
file pins what the thin Typer commands print, their exit codes, and the edge inputs. Everything runs
against temp dirs and fixture dbs built from the real schema.sql, reached via KNOWLEDGE_PRIVATE_DIR.
Nothing here can touch the live db or the real knowledge-private data.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest
import typer.main

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli_parity  # noqa: E402
import knowledge  # noqa: E402
from test_knowledge import Env as BaseEnv, ok  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

REV_COLS = ("fact_id", "source_key", "revision", "changed_at", "changed_via", "session_id", "change_reason",
            "statement", "trust_level", "trust_rationale", "status", "visibility", "superseded_by",
            "recheck_by", "recheck_rationale", "volatility", "notes")


class Env(BaseEnv):
    """test_knowledge's fixture db plus source keys and revisions:
    fact 1 (key k-one): rev 1 2026-01-01, rev 2 2026-06-01T12:00:00+00:00 (statement + trust change);
    fact 2 (key k-two): rev 1 only, 2026-03-01; fact 3 has no source_key and no revisions."""

    def __init__(self, root, with_db=True):
        super().__init__(root, with_db=with_db)
        self.data_dir = os.path.join(self.root, "data")
        self.env = {**self.env, **GIT_ENV}
        if with_db:
            self.sql("UPDATE facts SET source_key='k-one' WHERE id=1")
            self.sql("UPDATE facts SET source_key='k-two' WHERE id=2")
            self.rev(1, "k-one", 1, "2026-01-01T00:00:00+00:00", "migrate", "Old protein statement", "medium")
            self.rev(1, "k-one", 2, "2026-06-01T12:00:00+00:00", "cli", "Protein intake of 1.6 g/kg maximizes hypertrophy",
                     "high", reason="new meta-analysis", session="s-1")
            self.rev(2, "k-two", 1, "2026-03-01T00:00:00+00:00", "migrate", "Creatine monohydrate is effective", "verified")

    def sql(self, q, *params):
        con = sqlite3.connect(self.db)
        con.execute(q, params)
        con.commit()
        con.close()

    def rev(self, fact_id, key, n, at, via, statement, trust, reason=None, session=None, status="active"):
        self.sql(f"INSERT INTO fact_revisions ({','.join(REV_COLS)}) VALUES ({','.join('?' * len(REV_COLS))})",
                 fact_id, key, n, at, via, session, reason, statement, trust, None, status, "private", None, None, None, None, None)

    def claim(self, claim_id, statement, *fact_ids, inference=None):
        self.sql("INSERT INTO claims (id, statement, inference_type) VALUES (?, ?, ?)", claim_id, statement, inference)
        for f in fact_ids:
            self.sql("INSERT INTO claim_facts (claim_id, fact_id) VALUES (?, ?)", claim_id, f)

    def rules_file(self):
        return os.path.join(self.data_dir, "privacy_rules.json")

    def rules(self):
        with open(self.rules_file()) as f:
            return json.load(f)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ====================================================================== history

def test_history_by_id_text(env):
    out = ok(env.cli("history", "1"))
    lines = out.splitlines()
    assert lines[0] == "Fact #1  [protein]  source_key=k-one  (2 revisions)"
    assert "rev 1  2026-01-01T00:00:00+00:00  via=migrate" in out
    assert "rev 2  2026-06-01T12:00:00+00:00  via=cli  session=s-1" in out
    assert "reason: new meta-analysis" in out
    assert "statement: Old protein statement" in out  # revision 1 shows the full state
    assert "statement: 'Old protein statement' -> 'Protein intake of 1.6 g/kg maximizes hypertrophy'" in out
    assert "trust_level: 'medium' -> 'high'" in out
    assert out.index("rev 1") < out.index("rev 2")


def test_history_by_source_key_equals_by_id(env):
    assert ok(env.cli("history", "k-one")) == ok(env.cli("history", "1"))


def test_history_json_is_the_library_result(env):
    rows = json.loads(ok(env.cli("history", "1", "--json")))
    assert [r["revision"] for r in rows] == [1, 2]
    assert rows[1]["change_reason"] == "new meta-analysis" and rows[1]["subject"] == "protein"
    assert json.loads(ok(env.cli("history", "k-one", "--json"))) == rows


def test_history_single_revision_says_so(env):
    assert "(1 revision)" in ok(env.cli("history", "2"))


@pytest.mark.parametrize("ref", ["999", "no-such-key", "3", "0", "  ", "", "' OR 1=1 --", "1.5", "１"])
def test_history_unknown_or_odd_refs_are_one_line_errors(env, ref):
    """3 exists but has no revisions; the others do not exist at all. Never a traceback."""
    r = env.cli("history", ref)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert r.stdout == "" and r.stderr.startswith("error: ") and r.stderr.count("\n") == 1
    assert "Traceback" not in r.stderr


def test_history_negative_number_after_double_dash_is_a_key_lookup(env):
    r = env.cli("history", "--", "-5")
    assert r.returncode == 1 and r.stderr.startswith("error: no revision history")


def test_history_numeric_text_is_an_id_not_a_key(env):
    """An all-digit argument is always a fact id, even if some fact's source_key looks like that number."""
    env.sql("UPDATE facts SET source_key='2' WHERE id=1")
    env.sql("UPDATE fact_revisions SET source_key='2' WHERE fact_id=1")
    assert "Fact #2" in ok(env.cli("history", "2"))


def test_history_missing_db(tmp_path):
    r = Env(tmp_path, with_db=False).cli("history", "1")
    assert r.returncode == 1 and r.stderr.startswith("error: ") and "build" in r.stderr


# ====================================================================== show --as-of

def test_show_as_of_picks_the_revision_in_force(env):
    out = ok(env.cli("show", "1", "--as-of", "2026-03-15"))
    assert out.splitlines()[0] == "Fact #1  [protein]  as of 2026-03-15  (revision 1, changed 2026-01-01T00:00:00+00:00 via migrate)"
    assert "trust=medium" in out and "Old protein statement" in out


def test_show_as_of_after_the_change(env):
    out = ok(env.cli("show", "1", "--as-of", "2026-12-31"))
    assert "(revision 2," in out and "trust=high" in out and "Reason: new meta-analysis" in out


def test_show_as_of_date_means_end_of_that_day(env):
    assert "(revision 2," in ok(env.cli("show", "1", "--as-of", "2026-06-01"))
    assert "(revision 1," in ok(env.cli("show", "1", "--as-of", "2026-05-31"))


@pytest.mark.parametrize("when,rev", [
    ("2026-06-01T11:59:59+00:00", 1), ("2026-06-01T12:00:00+00:00", 2), ("2026-06-01T12:00:00Z", 2),
    ("2026-06-01T14:00:00+02:00", 2), ("2026-06-01T13:59:59+02:00", 1), ("2026-06-01T11:59:59", 1)])
def test_show_as_of_timestamp_formats(env, when, rev):
    assert f"(revision {rev}," in ok(env.cli("show", "1", "--as-of", when))


def test_show_as_of_json(env):
    r = json.loads(ok(env.cli("show", "1", "--as-of", "2026-03-15", "--json")))
    assert r["revision"] == 1 and r["fact_id"] == 1 and r["trust_level"] == "medium"


def test_show_as_of_before_the_fact_existed(env):
    r = env.cli("show", "1", "--as-of", "2025-12-31")
    assert (r.returncode, r.stdout) == (1, "")
    assert r.stderr == "error: fact 1 did not exist yet at 2025-12-31\n"
    assert env.cli("show", "1", "--as-of", "2025-12-31", "--json").returncode == 1


@pytest.mark.parametrize("bad", ["07/01/2026", "tomorrow", "2026-13-45", "", "   ", "2026-02-30", "x" * 5000])
def test_show_as_of_bad_date_is_one_line_error(env, bad):
    r = env.cli("show", "1", "--as-of", bad)
    assert r.returncode == 1 and r.stdout == ""
    assert r.stderr.startswith("error: ") and r.stderr.count("\n") == 1 and "Traceback" not in r.stderr


def test_show_as_of_unknown_id_and_fact_without_revisions(env):
    r = env.cli("show", "999", "--as-of", "2026-01-01")
    assert (r.returncode, r.stderr) == (1, "error: no fact with id 999\n")
    r = env.cli("show", "3", "--as-of", "2026-01-01")  # exists, no revision rows
    assert r.returncode == 1 and r.stderr.startswith("error: ") and "no revision history" in r.stderr


def test_plain_show_unchanged_without_as_of(env):
    assert "Source key: k-one" in ok(env.cli("show", "1"))


# ====================================================================== audit-claims

def test_audit_claims_clean_exits_zero(env):
    env.claim(1, "Creatine works", 2)
    r = env.cli("audit-claims")
    assert (r.returncode, r.stdout, r.stderr) == (0, "No stale premises.\n", "")


def test_audit_claims_no_claims_at_all(env):
    r = env.cli("audit-claims")
    assert (r.returncode, r.stdout) == (0, "No stale premises.\n")


def test_audit_claims_rows_exit_one(env):
    env.claim(1, "Protein timing matters", 4, inference="inductive")
    env.claim(2, "Coltrane thing", 5)
    r = env.cli("audit-claims")
    assert r.returncode == 1, r.stderr
    assert "claim #1" in r.stdout and "fact #4" in r.stdout and "superseded" in r.stdout
    assert "claim #2" in r.stdout and "fact #5" in r.stdout and "retracted" in r.stdout
    assert "Protein timing matters" in r.stdout and "Old claim about protein timing" in r.stdout


def test_audit_claims_past_recheck_by(env):
    env.sql("UPDATE facts SET recheck_by='2020-01-01' WHERE id=1")
    env.claim(1, "c", 1)
    r = env.cli("audit-claims")
    assert r.returncode == 1 and "past_recheck_by" in r.stdout and "2020-01-01" in r.stdout


def test_audit_claims_json_shape_and_exit_code(env):
    env.claim(1, "c", 4)
    env.sql("UPDATE facts SET recheck_by='after surgery' WHERE id=2")
    env.claim(2, "d", 2)
    r = env.cli("audit-claims", "--json")
    assert r.returncode == 1
    data = json.loads(r.stdout)
    assert [x["fact_id"] for x in data["stale_premises"]] == [4]
    assert data["stale_premises"][0]["reason"] == "superseded"
    assert data["unparseable_rechecks"] == [{"fact_id": 2, "recheck_by": "after surgery"}]


def test_audit_claims_json_clean_exits_zero(env):
    r = env.cli("audit-claims", "--json")
    assert r.returncode == 0 and json.loads(r.stdout) == {"stale_premises": [], "unparseable_rechecks": []}


def test_audit_claims_unparseable_rechecks_are_a_separate_note_not_a_failure(env):
    env.sql("UPDATE facts SET recheck_by='next panel' WHERE id=2")
    env.claim(1, "c", 2)
    r = env.cli("audit-claims")
    assert r.returncode == 0  # never flagged, so it must not fail the audit
    assert r.stdout == "No stale premises.\n"
    assert "note:" in r.stderr and "#2" in r.stderr and "next panel" in r.stderr


def test_audit_claims_missing_db_and_old_schema(tmp_path):
    r = Env(tmp_path / "a", with_db=False).cli("audit-claims")
    assert r.returncode == 1 and r.stderr.startswith("error: ") and "build" in r.stderr
    e = Env(tmp_path / "b")
    e.sql("DROP VIEW IF EXISTS v_claims_with_stale_premises")
    e.sql("DROP TABLE claim_facts")
    r = e.cli("audit-claims")
    assert r.returncode == 1 and r.stderr.startswith("error: ") and "Traceback" not in r.stderr


# ====================================================================== parity registry

@pytest.fixture(scope="module")
def group():
    return typer.main.get_command(knowledge.app)


NEW_ACTIONS = {
    "get_history": ("history",),
    "audit_claims": ("audit-claims",),
    "privacy_check": ("privacy", "check"),
    "privacy_rules": ("privacy", "rules"),
    "privacy_tag": ("privacy", "tag"),
    "privacy_untag": ("privacy", "untag"),
    "privacy_add_keyword": ("privacy", "add-keyword"),
    "privacy_remove_keyword": ("privacy", "remove-keyword"),
}


def test_new_actions_are_registered_and_resolve(group):
    for name, path in NEW_ACTIONS.items():
        assert cli_parity.ACTIONS.get(name) == path, name
    assert cli_parity.actions_without_command(cli_parity.ACTIONS, group) == []


@pytest.mark.parametrize("path", [("history",), ("show",), ("audit-claims",), ("privacy", "check"), ("privacy", "rules")])
def test_new_read_commands_have_json_flag(group, path):
    cmd = cli_parity.resolve_command(group, path)
    assert "--json" in {o for p in cmd.params for o in p.opts}


@pytest.mark.parametrize("path", [("privacy", "tag"), ("privacy", "untag"), ("privacy", "add-keyword"), ("privacy", "remove-keyword")])
def test_privacy_edit_commands_have_allow_dirty_and_dry_run(group, path):
    cmd = cli_parity.resolve_command(group, path)
    opts = {o for p in cmd.params for o in p.opts}
    assert {"--allow-dirty", "--dry-run"} <= opts
