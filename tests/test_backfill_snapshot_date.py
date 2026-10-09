"""The `measurements` client source's one-time snapshot backfill: writes measurements_snapshot.json once, never
regenerates it, and refuses to touch a malformed one. (The core date backfill no longer knows about it.)"""
import json
import os
import subprocess
import sys

import pytest

from test_add_fact import Env, REPO
from test_backfill_dates import put, read

SNAP = "measurements_snapshot.json"


@pytest.fixture
def world(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", e.private)
    for m in ("paths", "local_paths", "revisions", "revisions_store", "snapshot_date", "backfill_snapshot_date"):
        sys.modules.pop(m, None)
    monkeypatch.syspath_prepend(REPO)
    return e


def run(world, data_dir, *flags):
    return subprocess.run([sys.executable, "-m", "client.backfill_snapshot_date", "--data-dir", data_dir, *flags],
                          env=world.env, capture_output=True, text=True, cwd=REPO)


def test_dry_run_is_the_default_and_writes_nothing(world, tmp_path):
    d = str(tmp_path / "copy")
    os.makedirs(d)
    r = run(world, d)
    assert r.returncode == 0 and "would be written" in r.stdout and "dry run" in r.stdout
    assert os.listdir(d) == []


def test_apply_writes_the_legacy_date_once(world, tmp_path):
    d = str(tmp_path / "copy")
    os.makedirs(d)
    assert run(world, d, "--apply").returncode == 0
    assert json.loads(read(d, SNAP)) == {"synced_at": "2026-09-11"}
    again = run(world, d, "--apply")
    assert again.returncode == 0 and "already present" in again.stdout


def test_the_snapshot_file_is_never_regenerated(world, tmp_path):
    d = str(tmp_path / "copy")
    put(d, SNAP, '{"synced_at": "2026-05-05"}\n')
    assert run(world, d, "--apply").returncode == 0
    assert read(d, SNAP) == '{"synced_at": "2026-05-05"}\n'


@pytest.mark.parametrize("bad", ["{nope", "[]", '{"synced_at": null}', '{"synced_at": "soon"}', "{}"])
def test_a_malformed_snapshot_file_is_reported_and_left_alone(world, tmp_path, bad):
    d = str(tmp_path / "copy")
    put(d, SNAP, bad)
    r = run(world, d, "--apply")
    assert r.returncode == 1 and SNAP in r.stderr and read(d, SNAP) == bad


def test_both_flags_together_are_refused(world, tmp_path):
    assert run(world, str(tmp_path), "--apply", "--dry-run").returncode == 2
