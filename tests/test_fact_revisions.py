"""Issue #30: append-only fact revisions. Everything runs on temp dirs and in-memory databases;
nothing here reads the real knowledge-private data."""
import ast
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest

import clock
from test_add_fact import Env, REPO

T1 = "2026-10-01T08:00:00+00:00"
T2 = "2026-10-02T09:00:00+00:00"
T3 = "2026-10-03T10:00:00+00:00"
T4 = "2026-10-04T11:00:00+00:00"


def entry(key="k1", statement="Original statement.", **kw):
    e = {"source_key": key, "subject": "alpha", "statement": statement, "trust_level": "low",
         "is_original_claim": False, "is_personal": True, "date_added": T1, "visibility": "private",
         "freshness": "no-decay", "recheck_rationale": "no decay"}  # #7: every entry carries a freshness
    e.update(kw)
    return e


@pytest.fixture
def world(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", e.private)
    for m in ("paths", "local_paths", "_shared", "add_fact", "revisions"):
        sys.modules.pop(m, None)
    monkeypatch.syspath_prepend(REPO)

    def load(name):
        spec = importlib.util.spec_from_file_location("w_" + name, os.path.join(REPO, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    import revisions
    import add_fact

    class W:
        env = e
        rv = revisions
        af = add_fact
        mod04 = load("04_ingest_facts.py")
        mod11 = load("11_seed_general_facts.py")
        mod12 = load("12_apply_fact_revisions.py")
        log = os.path.join(e.data_dir, "fact_revisions.jsonl")

        @staticmethod
        def write(name, items):
            with open(os.path.join(e.data_dir, name), "w") as f:
                json.dump(items, f, indent=2)

        @classmethod
        def seed(cls, general=(), pilot=()):
            cls.write("pilot_facts.json", list(pilot))
            for i in range(1, 5):
                cls.write(f"facts_batch{i}.json", [])
            cls.write("general_facts.json", list(general))

        @classmethod
        def build(cls):
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON;")
            con.executescript(open(os.path.join(REPO, "schema.sql")).read())
            for m in (cls.mod04, cls.mod11, cls.mod12):
                m.DATA_DIR = e.data_dir
            cls.mod04.run(con)
            cls.mod11.run(con)
            cls.mod12.run(con)
            return con

        @classmethod
        def append(cls, key, changes, reason="because", via="cli", session_id=None, at=None):
            if at is None:
                return cls.rv.append_revision(key, changes, reason, via, session_id, data_dir=e.data_dir)
            with clock.frozen(at):
                return cls.rv.append_revision(key, changes, reason, via, session_id, data_dir=e.data_dir)

        @classmethod
        def log_lines(cls):
            with open(cls.log) as f:
                return f.read().splitlines()

    return W


# ---------------------------------------------------------------- source keys

def test_legacy_dates_are_what_backfill_dates_writes_and_no_longer_live_in_the_ingest_scripts(world):
    dates = dict(world.rv.ENTRY_FILES)
    assert dates["pilot_facts.json"] == dates["facts_batch3.json"] == "2026-09-11"
    assert dates["general_facts.json"] == "2026-09-26"
    import backfill_dates
    assert backfill_dates.FILE_DATES == dates
    assert not hasattr(world.mod04, "LEGACY_DATE_ADDED") and not hasattr(world.mod11, "LEGACY_DATE_ADDED")


def test_derive_keys_is_deterministic_keeps_explicit_and_suffixes_duplicates(world):
    items = [{"subject": "a", "statement": "S"}, {"subject": "a", "statement": " S "}, {"source_key": "mine", "subject": "a", "statement": "S"},
             {"subject": "a", "statement": "Other"}, {}]
    keys = world.rv.derive_keys(items, "f.json")
    assert keys == world.rv.derive_keys(items, "f.json")
    assert keys[2] == "mine"
    assert keys[1] == keys[0] + "-2"
    assert len(set(keys)) == 5 and all(world.af.SOURCE_KEY_RE.match(k) for k in keys)
    assert keys[0] != world.rv.derive_keys(items, "g.json")[0]  # the file name is part of the identity
    # a partly keyed list derives the same keys for the unkeyed entries
    partial = [dict(items[0], source_key="x"), items[1]]
    assert world.rv.derive_keys(partial, "f.json")[1] == keys[0]


def test_every_new_fact_gets_a_unique_valid_source_key(world):
    keys = {world.af.build_entry(world.af.NewFact("S.", "x", "low", no_decay=True, recheck_rationale="no decay"))["source_key"] for _ in range(50)}
    assert len(keys) == 50 and all(world.af.SOURCE_KEY_RE.match(k) for k in keys)


def test_a_supplied_source_key_is_used_and_a_bad_one_is_refused(world):
    f = world.af.NewFact("S.", "x", "low", no_decay=True, recheck_rationale="no decay", source_key="my-key")
    assert world.af.build_entry(f)["source_key"] == "my-key"
    errors, _ = world.af.validate_fact(world.af.NewFact("S.", "x", "low", no_decay=True, recheck_rationale="no decay", source_key="Bad Key!"))
    assert any("source_key" in e for e in errors)


def test_ingest_stores_the_key_and_an_implicit_revision_1(world):
    world.seed(general=[entry("k1", captured_via="mcp", session_id="s1", source_quote="q", captured_at=T1, trust_rationale="why",
                              notes="n", recheck_by="2027-01-01", recheck_rationale="rr", visibility="normal")])
    con = world.build()
    f = con.execute("SELECT id, source_key FROM facts").fetchone()
    assert f["source_key"] == "k1"
    (r,) = con.execute("SELECT * FROM fact_revisions").fetchall()
    assert (r["fact_id"], r["source_key"], r["revision"]) == (f["id"], "k1", 1)
    assert (r["changed_at"], r["changed_via"], r["session_id"], r["change_reason"]) == (T1, "mcp", "s1", "original entry")
    assert (r["statement"], r["trust_level"], r["trust_rationale"], r["status"], r["visibility"]) == \
        ("Original statement.", "low", "why", "active", "normal")
    assert (r["superseded_by"], r["recheck_by"], r["recheck_rationale"], r["freshness"], r["notes"]) == (None, "2027-01-01", "rr", "no-decay", "n")


def test_legacy_entries_without_a_key_get_a_derived_key_and_their_backfilled_date(world):
    legacy = {"subject": "alpha", "statement": "Old fact.", "trust_level": "high"}
    world.seed(pilot=[dict(legacy)], general=[dict(legacy, freshness="no-decay", recheck_rationale="no decay")])  # #7: only the pilot entry is legacy
    import backfill_dates
    backfill_dates.backfill_dates(world.env.data_dir, apply=True)  # #35: the date lives in the data now
    con = world.build()
    rows = con.execute("SELECT f.source_key, r.changed_at, r.changed_via FROM facts f JOIN fact_revisions r ON r.fact_id = f.id ORDER BY f.id").fetchall()
    assert [r["source_key"][:7] for r in rows] == ["legacy-", "legacy-"]
    assert rows[0]["source_key"] != rows[1]["source_key"]
    assert [(r["changed_at"], r["changed_via"]) for r in rows] == [("2026-09-11", "original"), ("2026-09-26", "original")]


def test_a_pending_status_on_an_entry_is_honored_and_a_bad_one_skipped(world, capsys):
    world.seed(general=[entry("a", status="pending"), entry("b", status="bogus"), entry("c")])
    con = world.build()
    assert [(r["source_key"], r["status"]) for r in con.execute("SELECT source_key, status FROM facts ORDER BY id")] == [("a", "pending"), ("c", "active")]
    assert "invalid status" in capsys.readouterr().out


def test_duplicate_source_keys_fail_the_build_with_the_key(world):
    world.seed(general=[entry("dup"), entry("dup", statement="Another.")])
    with pytest.raises(ValueError, match="duplicate source_key 'dup'"):
        world.build()


def test_the_same_key_in_two_files_is_refused(world):
    world.seed(pilot=[entry("same")], general=[entry("same", statement="Z.")])
    with pytest.raises(ValueError, match="duplicate source_key 'same'"):
        world.build()


def test_an_undated_entry_fails_the_build_naming_file_and_entry(world):
    world.seed(pilot=[{"subject": "a", "statement": "Old fact.", "trust_level": "high"}])
    with pytest.raises(ValueError, match=r"pilot_facts\.json\[0\].*no `date_added`.*backfill_dates"):
        world.build()


# ---------------------------------------------------------------- append_revision

def test_three_revisions_give_ordered_history_and_facts_shows_the_latest(world):
    world.seed(general=[entry("k1")])
    assert world.append("k1", {"trust_level": "high"}, "checked it", at=T2).ok
    assert world.append("k1", {"statement": "Reworded statement."}, "clearer", via="mcp", session_id="s9", at=T3).ok
    assert world.append("k1", {"status": "retracted"}, "was wrong", at=T4).ok
    con = world.build()
    hist = world.rv.get_history(con, "k1")
    assert [h["revision"] for h in hist] == [1, 2, 3, 4]
    assert [h["trust_level"] for h in hist] == ["low", "high", "high", "high"]
    assert [h["statement"] for h in hist] == ["Original statement."] * 2 + ["Reworded statement."] * 2
    assert [h["status"] for h in hist] == ["active", "active", "active", "retracted"]
    assert [h["changed_via"] for h in hist] == ["original", "cli", "mcp", "cli"]
    assert hist[2]["session_id"] == "s9" and hist[3]["change_reason"] == "was wrong"
    f = con.execute("SELECT statement, trust_level, status FROM facts").fetchone()
    assert tuple(f) == ("Reworded statement.", "high", "retracted")
    assert [h["revision"] for h in world.rv.get_history(con, 1)] == [1, 2, 3, 4]  # by fact id too


def test_facts_fts_follows_the_latest_revision(world):
    world.seed(general=[entry("k1", statement="Zebra stripes.")])
    world.append("k1", {"statement": "Okapi stripes."}, "r", at=T2)
    con = world.build()
    assert con.execute("SELECT COUNT(*) FROM facts_fts WHERE facts_fts MATCH 'zebra'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM facts_fts WHERE facts_fts MATCH 'okapi'").fetchone()[0] == 1


def test_as_of_between_revisions_returns_the_intermediate_state(world):
    world.seed(general=[entry("k1")])
    world.append("k1", {"trust_level": "high"}, "r", at=T2)
    world.append("k1", {"status": "retracted"}, "r", at=T4)
    con = world.build()
    asof = lambda d: world.rv.get_fact_as_of(con, "k1", d)
    assert asof("2026-09-30") is None                                  # before the fact existed
    assert asof("2026-10-01")["revision"] == 1                          # a date means the end of that day
    assert (asof("2026-10-02")["revision"], asof("2026-10-02")["trust_level"]) == (2, "high")
    assert asof("2026-10-03")["status"] == "active"                    # between rev 2 and rev 3
    assert asof("2026-10-04")["status"] == "retracted"
    assert asof("2026-10-04T10:59:59+00:00")["revision"] == 2          # a full timestamp is exact
    assert asof("2099-01-01")["revision"] == 3
    assert world.rv.get_fact_as_of(con, 1, "2026-10-03")["revision"] == 2
    assert world.rv.get_fact_as_of(con, "nope", "2026-10-03") is None
    assert world.rv.get_history(con, "nope") == [] and world.rv.get_history(con, 999) == []
    for bad in ("yesterday", "2026-13-40", None):
        with pytest.raises(ValueError):
            asof(bad)
    with pytest.raises(TypeError):
        world.rv.get_history(con, 1.5)


def test_history_and_as_of_accept_a_db_path(world, tmp_path):
    world.seed(general=[entry("k1")])
    con = world.build()
    path = str(tmp_path / "k.db")
    disk = sqlite3.connect(path)
    con.backup(disk)
    disk.close()
    assert [h["revision"] for h in world.rv.get_history(path, "k1")] == [1]
    assert world.rv.get_fact_as_of(path, "k1", "2026-12-01")["revision"] == 1


def test_original_entry_bytes_are_unchanged_by_any_append(world):
    world.seed(general=[entry("k1")], pilot=[entry("k0", statement="Pilot.")])
    before = {n: open(os.path.join(world.env.data_dir, n), "rb").read() for n in os.listdir(world.env.data_dir)}
    assert world.append("k1", {"trust_level": "high"}, "r").ok
    assert world.append("k0", {"notes": "n"}, "r").ok
    after = {n: open(os.path.join(world.env.data_dir, n), "rb").read() for n in before}
    assert after == before


def test_a_revision_line_is_a_full_snapshot_with_fixed_key_order(world):
    world.seed(general=[entry("k1", notes="keep me")])
    r = world.append("k1", {"trust_level": "high"}, "r", at=T2)
    assert r.ok and r.revision["revision"] == 2
    (line,) = world.log_lines()
    rec = json.loads(line)
    assert list(rec) == list(world.rv.REVISION_KEYS)
    assert rec["notes"] == "keep me" and rec["statement"] == "Original statement." and rec["session_id"] is None
    assert rec["changed_at"] == T2 and rec["changed_via"] == "cli"
    world.append("k1", {"notes": "new"}, "r", at=T3)
    assert list(json.loads(world.log_lines()[1])) == list(world.rv.REVISION_KEYS)
    assert json.loads(world.log_lines()[1])["trust_level"] == "high"  # carried over from revision 2


def test_append_uses_the_clock_helper(world):
    world.seed(general=[entry("k1")])
    with clock.frozen("2026-10-05T01:02:03+00:00"):
        r = world.rv.append_revision("k1", {"notes": "x"}, "r", "cli", data_dir=world.env.data_dir)
    assert r.revision["changed_at"] == "2026-10-05T01:02:03+00:00"


@pytest.mark.parametrize("changes,reason,via,session,needle", [
    ({}, "r", "cli", None, "non-empty dict"),
    (None, "r", "cli", None, "non-empty dict"),
    ({"subject": "x"}, "r", "cli", None, "cannot change"),
    ({"source_key": "x"}, "r", "cli", None, "cannot change"),
    ({"notes": "x"}, "", "cli", None, "reason is required"),
    ({"notes": "x"}, "   ", "cli", None, "reason is required"),
    ({"notes": "x"}, "r", "Not Kebab", None, "via"),
    ({"notes": "x"}, "r", "cli", "  ", "session_id"),
    ({"statement": "   "}, "r", "cli", None, "statement"),
    ({"statement": None}, "r", "cli", None, "statement"),
    ({"trust_level": "certain"}, "r", "cli", None, "trust_level"),
    ({"status": "deleted"}, "r", "cli", None, "status"),
    ({"visibility": "public"}, "r", "cli", None, "visibility"),
    ({"superseded_by": "ghost"}, "r", "cli", None, "not an existing source_key"),
    ({"superseded_by": "k1"}, "r", "cli", None, "the fact itself"),
    ({"notes": 5}, "r", "cli", None, "notes"),
    ({"trust_level": "low"}, "r", "cli", None, "no field would change"),
])
def test_bad_appends_are_refused_with_a_message_and_write_nothing(world, changes, reason, via, session, needle):
    world.seed(general=[entry("k1"), entry("k2", statement="Other.")])
    r = world.rv.append_revision("k1", changes, reason, via, session, data_dir=world.env.data_dir)
    assert not r.ok and any(needle in e for e in r.errors), r.errors
    assert not os.path.exists(world.log)


def test_unknown_source_key_is_refused(world):
    world.seed(general=[entry("k1")])
    r = world.append("nope", {"notes": "x"})
    assert not r.ok and "unknown source_key" in r.errors[0]
    assert not os.path.exists(world.log)


def test_missing_data_files_mean_unknown_key_not_a_crash(world):
    r = world.append("k1", {"notes": "x"})
    assert not r.ok and "unknown source_key" in r.errors[0]


def test_superseded_by_a_real_key_resolves_in_facts(world):
    world.seed(general=[entry("old"), entry("new", statement="Newer.")])
    assert world.append("old", {"status": "superseded", "superseded_by": "new"}, "replaced", at=T2).ok
    con = world.build()
    r = con.execute("SELECT o.status, n.statement AS ns FROM facts o JOIN facts n ON n.id = o.superseded_by_fact_id WHERE o.source_key = 'old'").fetchone()
    assert (r["status"], r["ns"]) == ("superseded", "Newer.")


def test_a_clock_that_went_backwards_is_refused_not_written(world):
    world.seed(general=[entry("k1")])
    assert world.append("k1", {"notes": "a"}, "r", at=T3).ok
    r = world.append("k1", {"notes": "b"}, "r", at=T2)
    assert not r.ok and "clock went backwards" in r.errors[0]
    assert len(world.log_lines()) == 1
    world.build()  # and the log is still valid


def test_an_append_before_the_original_entry_date_is_refused(world):
    world.seed(general=[entry("k1")])
    r = world.append("k1", {"notes": "a"}, "r", at="2026-09-01T00:00:00+00:00")
    assert not r.ok and "backwards" in r.errors[0]


def test_appending_with_a_corrupt_log_refuses_and_names_the_line(world):
    world.seed(general=[entry("k1")])
    world.append("k1", {"notes": "a"}, "r", at=T2)
    with open(world.log, "a") as f:
        f.write("{not json\n")
    before = open(world.log, "rb").read()
    r = world.append("k1", {"notes": "b"}, "r", at=T3)
    assert not r.ok and ":2:" in r.errors[0]
    assert open(world.log, "rb").read() == before


def test_a_failure_on_the_second_append_leaves_the_first_intact_and_no_litter(world, monkeypatch):
    world.seed(general=[entry("k1")])
    assert world.append("k1", {"notes": "a"}, "r", at=T2).ok
    before = open(world.log, "rb").read()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    r = world.append("k1", {"notes": "b"}, "r", at=T3)
    monkeypatch.undo()
    assert not r.ok and "disk full" in r.errors[0]
    assert open(world.log, "rb").read() == before
    assert sorted(os.listdir(world.env.data_dir)) == sorted(
        ["pilot_facts.json", "facts_batch1.json", "facts_batch2.json", "facts_batch3.json", "facts_batch4.json",
         "general_facts.json", "fact_revisions.jsonl"])
    assert world.append("k1", {"notes": "b"}, "r", at=T3).ok  # and it recovers
    assert [json.loads(l)["revision"] for l in world.log_lines()] == [2, 3]


def test_no_lock_or_temp_file_lands_in_the_data_dir(world):
    world.seed(general=[entry("k1")])
    world.append("k1", {"notes": "a"}, "r")
    assert not [n for n in os.listdir(world.env.data_dir) if n.endswith((".lock", ".tmp"))]


def test_a_log_without_a_trailing_newline_gets_one_before_the_next_line(world):
    world.seed(general=[entry("k1")])
    world.append("k1", {"notes": "a"}, "r", at=T2)
    with open(world.log) as f:
        text = f.read()
    with open(world.log, "w") as f:
        f.write(text.rstrip("\n"))
    assert world.append("k1", {"notes": "b"}, "r", at=T3).ok
    assert [json.loads(l)["revision"] for l in world.log_lines()] == [2, 3]


def test_concurrent_appends_from_several_processes_lose_nothing(world):
    world.seed(general=[entry("k1")])
    n = 8
    script = ("import sys, revisions; "
              "r = revisions.append_revision('k1', {'notes': sys.argv[2]}, 'r', 'cli', data_dir=sys.argv[1]); "
              "sys.exit(0 if r.ok else 1)")
    env = {**world.env.env, "KNOWLEDGE_FROZEN_NOW": T3, "PYTHONPATH": REPO}
    procs = [subprocess.Popen([sys.executable, "-c", script, world.env.data_dir, f"note-{i}"], env=env, cwd=REPO,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(n)]
    results = [p.communicate() + (p.returncode,) for p in procs]
    assert [r[2] for r in results] == [0] * n, results
    recs = [json.loads(l) for l in world.log_lines()]
    assert [r["revision"] for r in recs] == list(range(2, n + 2))
    assert {r["notes"] for r in recs} == {f"note-{i}" for i in range(n)}
    world.build()  # the interleaved log validates


# ---------------------------------------------------------------- build-time validation

def rec(key="k1", revision=2, at=T2, **kw):
    r = {"source_key": key, "revision": revision, "changed_at": at, "changed_via": "cli", "session_id": None,
         "change_reason": "r", "statement": "Original statement.", "trust_level": "low", "trust_rationale": None,
         "status": "active", "visibility": "private", "superseded_by": None, "recheck_by": None,
         "recheck_rationale": "no decay", "freshness": "no-decay", "kind": "unclassified", "valid_from": None, "valid_to": None, "notes": None}
    r.update(kw)
    return r


def write_log(world, *lines):
    with open(world.log, "w") as f:
        for l in lines:
            f.write((l if isinstance(l, str) else json.dumps(l)) + "\n")


def test_empty_missing_and_blank_line_logs_build_fine(world):
    world.seed(general=[entry("k1")])
    for content in (None, "", "\n", "   \n\n"):
        if content is None:
            if os.path.exists(world.log):
                os.remove(world.log)
        else:
            open(world.log, "w").write(content)
        con = world.build()
        assert con.execute("SELECT COUNT(*) FROM fact_revisions").fetchone()[0] == 1


def test_blank_lines_between_records_are_tolerated_and_line_numbers_still_count_them(world):
    world.seed(general=[entry("k1")])
    write_log(world, "", rec(), "", rec(revision=4))
    with pytest.raises(world.rv.RevisionError, match=r"fact_revisions\.jsonl:4: .*revision 4.*expected 3"):
        world.build()


@pytest.mark.parametrize("lines,pattern", [
    ([rec(revision=3)], r":1: revision 3 for 'k1' but expected 2.*gap or a duplicate"),
    ([rec(), rec(revision=4)], r":2: revision 4 .* expected 3"),
    ([rec(), rec()], r":2: revision 2 .* expected 3"),
    ([rec(revision=1)], r":1: .*revision 1 must be an integer >= 2"),
    ([rec(at=T3), rec(revision=3, at=T2)], r":2: changed_at .* earlier than the previous"),
    ([rec(at="2026-09-30T00:00:00+00:00")], r":1: changed_at .* earlier than the previous revision"),
    ([rec(key="ghost")], r":1: unknown source_key 'ghost'"),
    ([rec(superseded_by="ghost", status="superseded")], r":1: superseded_by 'ghost' is not an existing source_key"),
    ([rec(superseded_by="k1", status="superseded")], r":1: .*points at the fact itself"),
    (["{broken"], r":1: not valid JSON"),
    (["[1, 2]"], r":1: not a JSON object"),
    ([{"source_key": "k1", "revision": 2}], r":1: missing key"),
    ([dict(rec(), extra="x")], r":1: unknown key"),
    ([rec(status="deleted")], r":1: status 'deleted'"),
    ([rec(trust_level="sure")], r":1: trust_level"),
    ([rec(visibility="public")], r":1: visibility"),
    ([rec(statement="  ")], r":1: statement"),
    ([rec(at="2026-10-02")], r":1: changed_at .* UTC offset"),
    ([rec(change_reason="")], r":1: change_reason"),
    ([rec(revision=True)], r":1: revision"),
    ([rec(revision="2")], r":1: revision"),
])
def test_invalid_logs_fail_the_build_naming_the_line(world, lines, pattern):
    world.seed(general=[entry("k1")])
    write_log(world, *lines)
    with pytest.raises(world.rv.RevisionError, match=pattern):
        world.build()
    assert str(world.log) in str(pytest.raises(world.rv.RevisionError, world.build).value)


def test_step_12_is_registered_after_the_fact_loaders_and_raises_on_a_bad_log(world):
    import build
    world.seed(general=[entry("k1")])
    write_log(world, rec(revision=5))
    assert "12_apply_fact_revisions.py" in build.STEPS
    assert build.STEPS.index("12_apply_fact_revisions.py") > build.STEPS.index("11_seed_general_facts.py")
    with pytest.raises(world.rv.RevisionError):  # build.build wraps a step exception in BuildError(stage=step name)
        world.build()


def test_revisions_to_a_fact_in_either_ingested_file_apply(world):
    world.seed(pilot=[entry("p1", statement="Pilot fact.")], general=[entry("g1")])
    world.append("p1", {"trust_level": "verified"}, "r", at=T2)
    world.append("g1", {"trust_level": "disputed"}, "r", at=T2)
    con = world.build()
    assert {r["source_key"]: r["trust_level"] for r in con.execute("SELECT source_key, trust_level FROM facts")} == {"p1": "verified", "g1": "disputed"}


def test_untouched_facts_keep_exactly_one_revision_and_unchanged_content(world):
    world.seed(general=[entry("a"), entry("b", statement="B."), entry("c", statement="C.")])
    world.append("b", {"notes": "x"}, "r", at=T2)
    con = world.build()
    counts = {r[0]: r[1] for r in con.execute("SELECT source_key, COUNT(*) FROM fact_revisions GROUP BY source_key")}
    assert counts == {"a": 1, "b": 2, "c": 1}
    assert con.execute("SELECT statement, notes FROM facts WHERE source_key = 'a'").fetchone()[:] == ("Original statement.", None)


# ---------------------------------------------------------------- backfill

def run_backfill(world, data_dir, *flags):
    return subprocess.run([sys.executable, os.path.join(REPO, "backfill_source_keys.py"), "--data-dir", data_dir, *flags],
                          env=world.env.env, capture_output=True, text=True, cwd=REPO)


def legacy_files(world, data_dir):
    """The three layouts the real files have: indent=2, compact one-line objects, tabs-and-no-final-newline."""
    os.makedirs(data_dir, exist_ok=True)
    a = [{"subject": "a", "statement": "One.", "trust_level": "low"}, {"subject": "a", "statement": "Two é.", "trust_level": "high", "notes": "n"}]
    b = [{"subject": "b", "statement": "Three.", "trust_level": "low"}]
    c = [{"subject": "c", "statement": "Same.", "trust_level": "low"}, {"subject": "c", "statement": "Same.", "trust_level": "low"}]
    files = {
        "pilot_facts.json": json.dumps(a, indent=2, ensure_ascii=False) + "\n",
        "facts_batch1.json": json.dumps(b),
        "facts_batch2.json": json.dumps(c, indent="\t"),
        "facts_batch3.json": "[]",
        "facts_batch4.json": "[ ]\n",
        "general_facts.json": '[\n  {"subject": "g", "statement": "Mixed.", "trust_level": "low"},\n  {\n    "subject": "g", "statement": "Mixed 2.", "trust_level": "low"\n  }\n]\n',
    }
    for n, t in files.items():
        with open(os.path.join(data_dir, n), "w", encoding="utf-8", newline="") as f:
            f.write(t)
    return files


def test_dry_run_is_the_default_and_writes_nothing(world, tmp_path):
    d = str(tmp_path / "copy")
    before = legacy_files(world, d)
    r = run_backfill(world, d)
    assert r.returncode == 0, r.stderr
    assert "dry run" in r.stdout and "nothing written" in r.stdout
    assert {n: open(os.path.join(d, n), encoding="utf-8", newline="").read() for n in before} == before
    r = run_backfill(world, d, "--dry-run")
    assert r.returncode == 0 and "dry run" in r.stdout
    assert run_backfill(world, d, "--apply", "--dry-run").returncode != 0


def test_apply_adds_only_the_key_and_every_other_byte_is_preserved(world, tmp_path):
    import re
    d = str(tmp_path / "copy")
    before = legacy_files(world, d)
    r = run_backfill(world, d, "--apply")
    assert r.returncode == 0, r.stderr
    key = r'"source_key": "legacy-[0-9a-f]{10}(?:-\d+)?"'
    total = 0
    for n, old in before.items():
        new = open(os.path.join(d, n), encoding="utf-8", newline="").read()
        stripped = re.sub(r"\n[ \t]*" + key + ",|" + key + ",? ", "", new)
        assert stripped == old, n
        total += len(re.findall(key, new))
        old_items, new_items = json.loads(old), json.loads(new)
        assert [{k: v for k, v in i.items() if k != "source_key"} for i in new_items] == old_items
    assert total == 2 + 1 + 2 + 2
    keys = [i["source_key"] for n in before for i in json.load(open(os.path.join(d, n)))]
    assert len(keys) == len(set(keys)) == 7


def test_apply_twice_changes_nothing_the_second_time_and_never_rewrites_keys(world, tmp_path):
    d = str(tmp_path / "copy")
    legacy_files(world, d)
    run_backfill(world, d, "--apply")
    snap = {n: (open(os.path.join(d, n), "rb").read(), os.stat(os.path.join(d, n)).st_mtime_ns) for n in os.listdir(d)}
    r = run_backfill(world, d, "--apply")
    assert r.returncode == 0 and "updated 0 entries" in r.stdout
    assert {n: (open(os.path.join(d, n), "rb").read(), os.stat(os.path.join(d, n)).st_mtime_ns) for n in os.listdir(d)} == snap


def test_existing_keys_are_left_alone_even_when_others_are_added(world, tmp_path):
    d = str(tmp_path / "copy")
    os.makedirs(d)
    text = json.dumps([{"source_key": "mine", "subject": "a", "statement": "One.", "trust_level": "low"},
                       {"subject": "a", "statement": "Two.", "trust_level": "low"}], indent=2) + "\n"
    open(os.path.join(d, "general_facts.json"), "w").write(text)
    run_backfill(world, d, "--apply")
    items = json.load(open(os.path.join(d, "general_facts.json")))
    assert items[0]["source_key"] == "mine" and items[1]["source_key"].startswith("legacy-")
    assert open(os.path.join(d, "general_facts.json")).read().count('"mine"') == 1


def test_the_derived_keys_equal_the_in_memory_keys_so_build_is_identical_before_and_after(world):
    d = "2026-09-11"
    items = [{"subject": "a", "statement": "One.", "trust_level": "low", "date_added": d}, {"subject": "a", "statement": "One.", "trust_level": "low", "date_added": d},
             {"subject": "b", "statement": "Two.", "trust_level": "high", "date_added": d}]
    world.seed(pilot=[dict(i) for i in items], general=[dict(items[2], freshness="no-decay", recheck_rationale="no decay")])
    before = world.build()
    keys_before = [tuple(r) for r in before.execute("SELECT id, source_key FROM facts ORDER BY id")]
    revs_before = [tuple(r) for r in before.execute("SELECT fact_id, source_key, revision, changed_at, statement FROM fact_revisions ORDER BY id")]
    assert len({k for _, k in keys_before}) == 4
    r = run_backfill(world, world.env.data_dir, "--apply")
    assert r.returncode == 0, r.stderr
    assert json.load(open(os.path.join(world.env.data_dir, "pilot_facts.json")))[0]["source_key"] == keys_before[0][1]
    after = world.build()
    assert [tuple(r) for r in after.execute("SELECT id, source_key FROM facts ORDER BY id")] == keys_before
    assert [tuple(r) for r in after.execute("SELECT fact_id, source_key, revision, changed_at, statement FROM fact_revisions ORDER BY id")] == revs_before


def test_revisions_written_before_the_backfill_still_resolve_after_it(world):
    world.seed(general=[{"subject": "a", "statement": "One.", "trust_level": "low", "date_added": "2026-09-26", "freshness": "no-decay", "recheck_rationale": "no decay"}])
    (key,) = [e["key"] for e in world.rv.load_entries(world.env.data_dir)]
    assert key.startswith("legacy-")
    assert world.append(key, {"trust_level": "high"}, "r", at=T2).ok
    run_backfill(world, world.env.data_dir, "--apply")
    con = world.build()
    assert [h["trust_level"] for h in world.rv.get_history(con, key)] == ["low", "high"]


def test_backfill_refuses_bad_input_without_writing(world, tmp_path):
    d = str(tmp_path / "copy")
    os.makedirs(d)
    open(os.path.join(d, "pilot_facts.json"), "w").write('[{"subject": "a", "statement": "X", "trust_level": "low", "source_key": "same"}]')
    open(os.path.join(d, "general_facts.json"), "w").write('[{"subject": "a", "statement": "Y", "trust_level": "low", "source_key": "same"}]')
    r = run_backfill(world, d, "--apply")
    assert r.returncode == 1 and "duplicate source_key" in r.stderr and "Traceback" not in r.stderr
    open(os.path.join(d, "general_facts.json"), "w").write("{not json")
    r = run_backfill(world, d, "--apply")
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert open(os.path.join(d, "general_facts.json")).read() == "{not json"


def test_extension_hook_can_add_more_keys_for_issue_35(world, tmp_path):
    import backfill_source_keys as bk
    d = str(tmp_path / "copy")
    os.makedirs(d)
    open(os.path.join(d, "pilot_facts.json"), "w").write(json.dumps([{"subject": "a", "statement": "One.", "trust_level": "low", "date_added": "keep"},
                                                                       {"subject": "a", "statement": "Two.", "trust_level": "low"}], indent=2) + "\n")
    adder = lambda entry, filename, index: {"date_added": "2026-09-11", "extra": index}
    assert bk.backfill(d, extra_adders=[adder], apply=True)["pilot_facts.json"] == 2
    one, two = json.load(open(os.path.join(d, "pilot_facts.json")))
    assert one["date_added"] == "keep" and one["extra"] == 0 and "source_key" in one
    assert two["date_added"] == "2026-09-11" and two["extra"] == 1
    assert bk.backfill(d, extra_adders=[adder], apply=True)["pilot_facts.json"] == 0


def test_backfill_cannot_lose_a_concurrent_add_fact_append(world, tmp_path):
    d = str(tmp_path / "copy")
    os.makedirs(d)
    legacy = [{"subject": "g", "statement": f"Legacy {i}.", "trust_level": "low", "freshness": "no-decay", "recheck_rationale": "no decay"} for i in range(3)]
    open(os.path.join(d, "general_facts.json"), "w").write(json.dumps(legacy, indent=2) + "\n")
    script = ("import sys, add_fact; "
              "r = add_fact.append_fact(add_fact.NewFact(sys.argv[2], 'g', 'low', no_decay=True, recheck_rationale='r'), data_path=sys.argv[1] + '/general_facts.json'); "
              "sys.exit(0 if r.ok else 1)")
    env = {**world.env.env, "PYTHONPATH": REPO}
    procs = [subprocess.Popen([sys.executable, "-c", script, d, f"New {i}."], env=env, cwd=REPO) for i in range(6)]
    procs.append(subprocess.Popen([sys.executable, os.path.join(REPO, "backfill_source_keys.py"), "--data-dir", d, "--apply"], env=env, cwd=REPO,
                                  stdout=subprocess.DEVNULL))
    assert [p.wait() for p in procs] == [0] * 7
    run_backfill(world, d, "--apply")
    items = json.load(open(os.path.join(d, "general_facts.json")))
    assert len(items) == 9
    assert all("source_key" in i for i in items)
    assert len({i["source_key"] for i in items}) == 9
    assert [i["statement"] for i in items[:3]] == [f"Legacy {i}." for i in range(3)]


# ---------------------------------------------------------------- callers must not discard the result

def test_no_caller_discards_the_result_of_append_revision():
    """append_revision returns RevisionResult(ok=False) instead of raising. A bare call statement
    would treat "refused" as "done"; this scans every module and test for that."""
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", ".claude", "__pycache__", "node_modules"}]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    f = node.value.func
                    called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if called == "append_revision":
                        offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    assert offenders == [], f"result of append_revision discarded at {offenders}"
