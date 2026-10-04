"""Issue #35: backfill_dates.py writes `date_added` into the legacy entries without changing any
stored date. Temp dirs only; nothing here touches the real knowledge-private data."""
import json
import os
import re
import subprocess
import sys

import pytest

from test_add_fact import Env, REPO
from test_fact_ingest import ingest  # noqa: F401  (fixture)

SNAP = "measurements_snapshot.json"


@pytest.fixture
def world(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", e.private)
    for m in ("paths", "local_paths", "_shared", "add_fact", "revisions", "backfill_source_keys",
              "backfill_dates", "snapshot_date"):
        sys.modules.pop(m, None)
    monkeypatch.syspath_prepend(REPO)
    return e


def run(world, data_dir, *flags):
    return subprocess.run([sys.executable, os.path.join(REPO, "backfill_dates.py"), "--data-dir", data_dir, *flags],
                          env=world.env, capture_output=True, text=True, cwd=REPO)


def put(d, name, text):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read(d, name):
    with open(os.path.join(d, name), encoding="utf-8", newline="") as f:
        return f.read()


def fixture_files(d):
    """The layouts the real files have: indent=2, indent=1, compact objects, tabs and no final newline, empty, mixed."""
    a = [{"subject": "a", "statement": "One.", "trust_level": "low"}, {"subject": "a", "statement": "Two é.", "trust_level": "high", "notes": "n"}]
    b = [{"subject": "b", "statement": "Three.", "trust_level": "low"}]
    c = [{"subject": "c", "statement": "Same.", "trust_level": "low"}, {"subject": "c", "statement": "Same.", "trust_level": "low"}]
    files = {
        "pilot_facts.json": json.dumps(a, indent=2, ensure_ascii=False) + "\n",
        "facts_batch1.json": json.dumps(b, indent=1),
        "facts_batch2.json": json.dumps(c, indent="\t"),
        "facts_batch3.json": "[]",
        "facts_batch4.json": "[ ]\n",
        "general_facts.json": '[\n  {"subject": "g", "statement": "Mixed.", "trust_level": "low"},\n  {\n    "subject": "g", "statement": "Mixed 2.", "trust_level": "low"\n  }\n]\n',
    }
    for n, t in files.items():
        put(d, n, t)
    return files


# ---------------------------------------------------------------- safety properties

def test_dry_run_is_the_default_and_writes_nothing(world, tmp_path):
    d = str(tmp_path / "copy")
    before = fixture_files(d)
    r = run(world, d)
    assert r.returncode == 0, r.stderr
    assert "dry run" in r.stdout and "would update 2 entries" in r.stdout
    assert {n: read(d, n) for n in before} == before and not os.path.exists(os.path.join(d, SNAP))
    assert run(world, d, "--dry-run").returncode == 0
    assert {n: read(d, n) for n in before} == before
    assert run(world, d, "--apply", "--dry-run").returncode != 0


def test_apply_inserts_the_legacy_date_and_nothing_else(world, tmp_path):
    d = str(tmp_path / "copy")
    before = fixture_files(d)
    r = run(world, d, "--apply")
    assert r.returncode == 0, r.stderr
    for name, old in before.items():
        new = read(d, name)
        old_items, new_items = json.loads(old), json.loads(new)
        want = "2026-09-26" if name == "general_facts.json" else "2026-09-11"
        for o, n in zip(old_items, new_items):
            assert n.pop("date_added") == want
            assert n == o and "source_key" not in n          # nothing else, in particular no source_key
        assert len(old_items) == len(new_items)
        if old_items:  # byte-preserving: taking the inserted key back out gives the old bytes
            assert new != old
        else:
            assert new == old                                 # empty files are not rewritten
    # layout preserved: first key stays first, trailing newline / indentation kept
    assert read(d, "pilot_facts.json").endswith("\n") and not read(d, "facts_batch2.json").endswith("\n")
    assert read(d, "pilot_facts.json").startswith('[\n  {\n    "date_added": "2026-09-11",\n    "subject"')
    assert read(d, "facts_batch2.json").startswith('[\n\t{\n\t\t"date_added": "2026-09-11",\n\t\t"subject"')
    assert read(d, "general_facts.json").startswith('[\n  {"date_added": "2026-09-26", "subject"')


def test_byte_exact_against_the_source_key_tool_layouts(world, tmp_path):
    """Stripping exactly the inserted text restores the original bytes (the tool also proves this itself)."""
    d = str(tmp_path / "copy")
    before = fixture_files(d)
    run(world, d, "--apply")
    for name, old in before.items():
        new = read(d, name)
        for line_style in (r'\n\s*"date_added": "[0-9-]+",', r' "date_added": "[0-9-]+",', r'"date_added": "[0-9-]+", '):
            new = re.sub(line_style, "", new)
        assert new == old, name


def test_apply_twice_is_a_noop_and_never_rewrites(world, tmp_path):
    d = str(tmp_path / "copy")
    fixture_files(d)
    run(world, d, "--apply")
    after = {n: read(d, n) for n in os.listdir(d)}
    mtimes = {n: os.stat(os.path.join(d, n)).st_mtime_ns for n in after}
    r = run(world, d, "--apply")
    assert r.returncode == 0 and "updated 0 entries" in r.stdout and "already present" in r.stdout
    assert {n: read(d, n) for n in os.listdir(d)} == after
    assert {n: os.stat(os.path.join(d, n)).st_mtime_ns for n in after} == mtimes


def test_a_half_backfilled_file_gets_only_the_missing_dates(world, tmp_path):
    d = str(tmp_path / "copy")
    items = [{"subject": "a", "statement": "One.", "trust_level": "low", "date_added": "2026-10-03T08:00:00+00:00"},
             {"subject": "a", "statement": "Two.", "trust_level": "low"},
             {"date_added": "2026-01-01", "subject": "a", "statement": "Three.", "trust_level": "low"}]
    put(d, "pilot_facts.json", json.dumps(items, indent=2) + "\n")
    r = run(world, d, "--apply")
    assert r.returncode == 0, r.stderr
    got = json.loads(read(d, "pilot_facts.json"))
    assert [e["date_added"] for e in got] == ["2026-10-03T08:00:00+00:00", "2026-09-11", "2026-01-01"]
    assert json.loads(read(d, "pilot_facts.json"))[0] == items[0]


def test_an_entry_with_a_date_but_no_source_key_is_left_alone(world, tmp_path):
    d = str(tmp_path / "copy")
    text = json.dumps([{"subject": "a", "statement": "One.", "trust_level": "low", "date_added": "2026-02-02"}], indent=2) + "\n"
    put(d, "facts_batch1.json", text)
    assert run(world, d, "--apply").returncode == 0
    assert read(d, "facts_batch1.json") == text


def test_an_existing_source_key_survives_and_none_is_invented(world, tmp_path):
    d = str(tmp_path / "copy")
    put(d, "pilot_facts.json", json.dumps([{"source_key": "f-abc", "subject": "a", "statement": "One.", "trust_level": "low"}], indent=2))
    run(world, d, "--apply")
    (e,) = json.loads(read(d, "pilot_facts.json"))
    assert e["source_key"] == "f-abc" and e["date_added"] == "2026-09-11"


@pytest.mark.parametrize("bad", [None, "", "  ", "11/09/2026", "yesterday", 20260911, ["2026-09-11"]])
def test_a_present_but_invalid_date_is_reported_and_blocks_every_write(world, tmp_path, bad):
    d = str(tmp_path / "copy")
    ok = json.dumps([{"subject": "a", "statement": "One.", "trust_level": "low"}], indent=2)
    put(d, "pilot_facts.json", ok)
    put(d, "facts_batch1.json", json.dumps([{"subject": "b", "statement": "Two.", "trust_level": "low"},
                                            {"subject": "b", "statement": "Bad.", "trust_level": "low", "date_added": bad}]))
    r = run(world, d, "--apply")
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "facts_batch1.json[1]" in r.stderr
    assert read(d, "pilot_facts.json") == ok and not os.path.exists(os.path.join(d, SNAP))   # nothing written
    assert run(world, d).returncode == 1                                                      # dry run flags it too


def test_unparseable_json_and_non_array_files_fail_cleanly(world, tmp_path):
    d = str(tmp_path / "copy")
    put(d, "pilot_facts.json", "{not json")
    r = run(world, d, "--apply")
    assert r.returncode == 1 and "Traceback" not in r.stderr and read(d, "pilot_facts.json") == "{not json"
    put(d, "pilot_facts.json", '{"a": 1}')
    assert run(world, d, "--apply").returncode == 1


def test_missing_files_and_an_empty_dir_are_fine(world, tmp_path):
    d = str(tmp_path / "copy")
    os.makedirs(d)
    r = run(world, d, "--apply")
    assert r.returncode == 0, r.stderr
    assert json.loads(read(d, SNAP)) == {"synced_at": "2026-09-11"}


def test_the_snapshot_file_is_written_once_and_never_regenerated(world, tmp_path):
    d = str(tmp_path / "copy")
    fixture_files(d)
    put(d, SNAP, '{"synced_at": "2026-05-05"}\n')
    assert run(world, d, "--apply").returncode == 0
    assert read(d, SNAP) == '{"synced_at": "2026-05-05"}\n'


@pytest.mark.parametrize("bad", ["{nope", "[]", '{"synced_at": null}', '{"synced_at": "soon"}', "{}"])
def test_a_malformed_snapshot_file_blocks_every_write(world, tmp_path, bad):
    d = str(tmp_path / "copy")
    before = fixture_files(d)
    put(d, SNAP, bad)
    r = run(world, d, "--apply")
    assert r.returncode == 1 and SNAP in r.stderr
    assert {n: read(d, n) for n in before} == before and read(d, SNAP) == bad


def test_it_does_not_touch_the_source_key_machinery_unless_asked(world, tmp_path):
    import backfill_dates
    d = str(tmp_path / "copy")
    fixture_files(d)
    backfill_dates.backfill_dates(d, apply=True)
    assert all("source_key" not in e for n in ("pilot_facts.json", "facts_batch2.json") for e in json.loads(read(d, n)))


def test_source_keys_then_dates_equals_dates_then_source_keys(world, tmp_path):
    import backfill_source_keys
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    fixture_files(a)
    fixture_files(b)
    run(world, a, "--apply")
    backfill_source_keys.backfill(a, apply=True)
    backfill_source_keys.backfill(b, apply=True)
    run(world, b, "--apply")
    for n in ("pilot_facts.json", "facts_batch1.json", "general_facts.json"):
        assert json.loads(read(a, n)) == json.loads(read(b, n))
        assert all("source_key" in e and "date_added" in e for e in json.loads(read(a, n)))


def test_a_concurrent_add_fact_append_is_not_lost(world, tmp_path):
    d = str(tmp_path / "copy")
    legacy = [{"subject": "g", "statement": f"Legacy {i}.", "trust_level": "low"} for i in range(3)]
    put(d, "general_facts.json", json.dumps(legacy, indent=2) + "\n")
    script = ("import sys, add_fact; "
              "r = add_fact.append_fact(add_fact.NewFact(sys.argv[2], 'g', 'low'), data_path=sys.argv[1] + '/general_facts.json'); "
              "sys.exit(0 if r.ok else 1)")
    procs = [subprocess.Popen([sys.executable, "-c", script, d, f"New {i}."], env=world.env, cwd=REPO,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(4)]
    procs.append(subprocess.Popen([sys.executable, os.path.join(REPO, "backfill_dates.py"), "--data-dir", d, "--apply"],
                                  env=world.env, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    assert all(p.wait() == 0 for p in procs)
    run(world, d, "--apply")
    items = json.loads(read(d, "general_facts.json"))
    assert sorted(e["statement"] for e in items) == sorted([f"Legacy {i}." for i in range(3)] + [f"New {i}." for i in range(4)])
    assert all("date_added" in e for e in items)


# ---------------------------------------------------------------- the build sees the same dates

def test_after_the_backfill_ingest_gives_the_legacy_dates_and_keeps_stamped_ones(world, tmp_path, ingest):  # noqa: F811
    """Before #35 the build filled these from constants (04: 2026-09-11, 11: 2026-09-26); the backfill
    writes exactly those into the data, so the same dates come out (facts, last_reviewed_at, revision 1)."""
    legacy = [{"subject": "s", "statement": f"Legacy {i}.", "trust_level": "low"} for i in range(3)]
    legacy.append({"subject": "s", "statement": "Stamped.", "trust_level": "low", "date_added": "2026-10-03T12:00:00+00:00"})

    def dump(con):
        facts = [tuple(r) for r in con.execute("SELECT statement, date_added, last_reviewed_at FROM facts ORDER BY statement")]
        revs = [tuple(r) for r in con.execute(
            "SELECT f.statement, r.changed_at FROM fact_revisions r JOIN facts f ON f.id = r.fact_id ORDER BY f.statement")]
        return facts, revs

    ingest.write("pilot_facts.json", legacy)
    for i in range(1, 5):
        ingest.write(f"facts_batch{i}.json", [])
    ingest.write("general_facts.json", legacy)
    with pytest.raises(ValueError, match="no `date_added`"):      # not yet backfilled: loud, not guessed
        ingest.mod04.DATA_DIR = ingest.env.data_dir
        ingest.mod04.run(ingest.db())
    r = run(world, ingest.env.data_dir, "--apply")
    assert r.returncode == 0, r.stderr
    ingest.mod04.DATA_DIR = ingest.mod11.DATA_DIR = ingest.env.data_dir
    con04, con11 = ingest.db(), ingest.db()
    ingest.mod04.run(con04)
    ingest.mod11.run(con11)
    stamped = "2026-10-03T12:00:00+00:00"
    assert dump(con04) == ([(f"Legacy {i}.", "2026-09-11", "2026-09-11") for i in range(3)] + [("Stamped.", stamped, stamped)],
                           [(f"Legacy {i}.", "2026-09-11") for i in range(3)] + [("Stamped.", stamped)])
    assert dump(con11)[0][:3] == [(f"Legacy {i}.", "2026-09-26", "2026-09-26") for i in range(3)]
