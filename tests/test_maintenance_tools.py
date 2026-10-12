"""The maintenance tools as services: each `run(...)` takes its DataFiles / Inputs and the output streams, so it can
be invoked directly. Output, exit code and file effects are the same as the script's (compared against a subprocess)."""
import io
import json
import os
import shutil
import subprocess
import sys

import pytest

from test_add_fact import Env, REPO
from test_backfill_dates import fixture_files, put, read
import local_datafiles

TOOLS = {"dates": "ingest.backfill_dates", "keys": "ingest.backfill_source_keys",
         "snapshot": "client.backfill_snapshot_date"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", e.private)
    monkeypatch.syspath_prepend(REPO)
    return e


def script(world, module, data_dir, *flags):
    r = subprocess.run([sys.executable, "-m", module, "--data-dir", data_dir, *flags],
                       env=world.env, capture_output=True, text=True, cwd=REPO)
    return r.returncode, r.stdout, r.stderr


def direct(which, data_dir, apply):
    out, err = io.StringIO(), io.StringIO()
    if which == "dates":
        from ingest import backfill_dates
        code = backfill_dates.run(local_datafiles, data_dir, apply=apply, stdout=out, stderr=err)
    elif which == "keys":
        from ingest import backfill_source_keys
        code = backfill_source_keys.run(local_datafiles, data_dir, apply=apply, stdout=out, stderr=err)
    else:
        import local_inputs
        from client import backfill_snapshot_date
        code = backfill_snapshot_date.run(local_datafiles, local_inputs, data_dir, apply=apply, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("which", ["dates", "keys"])
@pytest.mark.parametrize("apply", [False, True])
def test_fact_file_backfills_run_directly_like_the_script(world, tmp_path, which, apply):
    a, b = str(tmp_path / "script"), str(tmp_path / "direct")
    fixture_files(a)
    shutil.copytree(a, b)
    flags = ["--apply"] if apply else []
    s_code, s_out, s_err = script(world, TOOLS[which], a, *flags)
    d_code, d_out, d_err = direct(which, b, apply)
    assert (d_code, d_err) == (s_code, s_err) == (0, "")
    assert d_out == s_out.replace(a, b)
    assert {n: read(b, n) for n in os.listdir(b)} == {n: read(a, n) for n in os.listdir(a)}


def test_a_bad_entry_is_reported_on_stderr_with_exit_1_and_nothing_is_written(world, tmp_path):
    d = str(tmp_path / "copy")
    fixture_files(d)
    put(d, "facts_batch1.json", json.dumps([{"subject": "b", "statement": "x", "trust_level": "low", "date_added": None}]))
    before = {n: read(d, n) for n in os.listdir(d)}
    code, out, err = direct("dates", d, True)
    assert code == 1 and out == "" and err.startswith("error: ") and "date_added" in err
    assert {n: read(d, n) for n in os.listdir(d)} == before
    assert script(world, TOOLS["dates"], d, "--apply")[0] == 1


@pytest.mark.parametrize("apply", [False, True])
def test_snapshot_backfill_runs_directly_like_the_script(world, tmp_path, apply):
    a, b = str(tmp_path / "script"), str(tmp_path / "direct")
    os.makedirs(a)
    os.makedirs(b)
    flags = ["--apply"] if apply else []
    s = script(world, TOOLS["snapshot"], a, *flags)
    d = direct("snapshot", b, apply)
    assert d == s
    assert os.listdir(a) == os.listdir(b)


def test_snapshot_backfill_reports_a_malformed_file_with_exit_1(world, tmp_path):
    d = str(tmp_path / "d")
    put(d, "measurements_snapshot.json", "{nope")
    code, out, err = direct("snapshot", d, True)
    assert code == 1 and out == "" and "measurements_snapshot.json" in err
    assert read(d, "measurements_snapshot.json") == "{nope"


def test_extracted_hashes_run_directly_with_an_in_memory_world(tmp_path):
    from ingest import backfill_extracted_hashes as bf
    d = str(tmp_path)
    put(d, "pilot_facts.json", json.dumps([{"subject": "a", "statement": "S.", "trust_level": "low",
                                              "origin_path": "/vault/a.md"},
                                             {"subject": "b", "statement": "T.", "trust_level": "low",
                                              "origin_path": "/vault/missing.md"}], indent=2) + "\n")

    class Inputs:
        def read_json(self, path):
            raise FileNotFoundError(path)

        def fingerprint(self, path):
            return {"content_sha256": "ab" * 32 if path.endswith("a.md") else None}

    out, err = io.StringIO(), io.StringIO()
    code = bf.run(local_datafiles, Inputs(), d, "/vault", apply=False, stdout=out, stderr=err)
    assert code == 0 and err.getvalue() == ""
    lines = out.getvalue().splitlines()
    assert lines[0] == "  pilot_facts.json: would update 1 entries"
    assert lines[1] == "  skipped pilot_facts.json[1]: origin file missing or unreadable: /vault/missing.md"
    assert lines[2].startswith("note: baselines are hashes") and lines[3].startswith("dry run: 1 entries would get")
    assert "extracted_from_sha256" not in read(d, "pilot_facts.json")
    code = bf.run(local_datafiles, Inputs(), d, "/vault", apply=True, stdout=io.StringIO(), stderr=io.StringIO())
    assert code == 0 and json.loads(read(d, "pilot_facts.json"))[0]["extracted_from_sha256"] == "ab" * 32


def test_clean_concerts_runs_directly_and_is_idempotent(tmp_path):
    from client import clean_concerts_csv, music_ingest_rules
    path = str(tmp_path / "concerts.csv")
    cols = music_ingest_rules.CONCERT_COLUMNS
    row = {c: "" for c in cols}
    row[cols[0]] = " Alpha "
    put(str(tmp_path), "concerts.csv", ",".join(cols) + "\n" + ",".join(row[c] for c in cols) + "\n" +
        ",".join(row[c] for c in cols) + "\n")
    out = io.StringIO()
    clean_concerts_csv.clean(local_datafiles, path, stdout=out)
    assert out.getvalue() == "[clean_concerts_csv] 2 rows -> 1 rows (1 duplicate/near-duplicate rows merged)\n"
    once = read(str(tmp_path), "concerts.csv")
    out = io.StringIO()
    clean_concerts_csv.clean(local_datafiles, path, stdout=out)
    assert read(str(tmp_path), "concerts.csv") == once and "1 rows -> 1 rows (0 " in out.getvalue()
