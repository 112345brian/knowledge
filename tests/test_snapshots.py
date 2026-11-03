"""Snapshots, restore, and restore-tables (increment 2c of docs/db-source-of-truth.md).

Fixture databases are built from the real schema in a temp dir; nothing here touches a real database.
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading

import pytest

import export_rules
import live_schema_store
import snapshot_rules as rules
from appctx import app, fixed_clock
from config_files import write_config
from test_db_export import make_db

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE = os.path.join(REPO, "knowledge.py")
T1 = "2026-10-01T08:00:00+00:00"
T2 = "2026-10-01T08:00:05+00:00"


class World:
    def __init__(self, root, clock=T1):
        self.root = str(root)
        self.db = make_db(os.path.join(self.root, "live.db"))
        self.snaps = os.path.join(self.root, "snapshots")
        self.export_dir = os.path.join(self.root, "export")
        self.clock = clock

    def container(self, clock=None):
        from locations import Locations
        return app(Locations(knowledge_db_dir=self.root, private_data_dir=os.path.join(self.root, "data")),
                   clock=fixed_clock(clock or self.clock))

    def snapshots(self, clock=None):
        return self.container(clock).db_snapshots(self.db, self.snaps)

    def restorer(self, snapshot=None, export=None):
        return self.container().db_restore_tables(self.db, snapshot, export, self.snaps)

    def export(self, **kw):
        return self.container().db_export(self.db, self.export_dir).export_database(**kw)

    def sql(self, q, *params, db=None):
        con = sqlite3.connect(db or self.db)
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute(q, params)
        con.commit()
        con.close()

    def rows(self, q, db=None):
        con = sqlite3.connect(db or self.db)
        try:
            return con.execute(q).fetchall()
        finally:
            con.close()

    def listing(self):
        return sorted(os.listdir(self.snaps)) if os.path.isdir(self.snaps) else []


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


# ---------------------------------------------------------------- snapshots

def test_a_snapshot_is_a_verified_complete_copy_named_by_time(w):
    result = w.snapshots().take_snapshot()
    assert os.path.basename(result.path) == "snapshot-20261001T080000.db" and result.pruned == ()
    assert w.listing() == ["snapshot-20261001T080000.db"]
    for q in ("SELECT COUNT(*) FROM facts", "SELECT COUNT(*) FROM artists", "SELECT COUNT(*) FROM facts_fts", "PRAGMA user_version"):
        assert w.rows(q, db=result.path) == w.rows(q)
    assert w.rows("PRAGMA integrity_check", db=result.path) == [("ok",)]


def test_a_snapshot_only_holds_what_was_committed(w):
    writer = sqlite3.connect(w.db)
    writer.execute("INSERT INTO artists (id, name) VALUES (99, 'Uncommitted')")      # open transaction, not committed
    try:
        path = w.snapshots().take_snapshot().path
    finally:
        writer.rollback()
        writer.close()
    assert w.rows("SELECT COUNT(*) FROM artists WHERE id = 99", db=path) == [(0,)]


def test_two_snapshots_in_one_second_never_overwrite_each_other(w):
    first = w.snapshots().take_snapshot().path
    before = open(first, "rb").read()
    w.sql("INSERT INTO artists (id, name) VALUES (5, 'New')")
    second = w.snapshots().take_snapshot().path
    assert os.path.basename(second) == "snapshot-20261001T080000-2.db" and open(first, "rb").read() == before
    assert w.rows("SELECT COUNT(*) FROM artists", db=second) == [(3,)] and w.rows("SELECT COUNT(*) FROM artists", db=first) == [(2,)]


def test_retention_keeps_the_newest_and_leaves_other_files_alone(w):
    open(os.path.join(w.root, "snapshots_note.txt"), "w").write("x")
    os.makedirs(w.snaps)
    open(os.path.join(w.snaps, "notes.txt"), "w").write("mine")
    open(os.path.join(w.snaps, "snapshot-bad.db"), "w").write("not a snapshot name")
    for i in range(4):
        w.snapshots(clock=f"2026-10-01T08:00:0{i}+00:00").take_snapshot(keep=2)
    assert w.listing() == ["notes.txt", "snapshot-20261001T080002.db", "snapshot-20261001T080003.db", "snapshot-bad.db"]
    assert w.snapshots().list_snapshots() == ["snapshot-20261001T080003.db", "snapshot-20261001T080002.db"]


def test_a_pruned_name_is_never_reused(w):
    for _ in range(3):
        w.snapshots().take_snapshot(keep=2)            # three in one second, the oldest pruned
    assert w.listing() == ["snapshot-20261001T080000-2.db", "snapshot-20261001T080000-3.db"]
    w.snapshots().take_snapshot(keep=2)
    assert w.listing() == ["snapshot-20261001T080000-3.db", "snapshot-20261001T080000-4.db"]


def test_keep_below_one_is_refused_before_anything_is_written(w):
    with pytest.raises(rules.SnapshotError, match="at least 1"):
        w.snapshots().take_snapshot(keep=0)
    assert w.listing() == []


def test_a_missing_database_fails_cleanly_and_leaves_no_partial_file(w, tmp_path):
    os.remove(w.db)
    with pytest.raises(rules.SnapshotError, match="no database"):
        w.snapshots().take_snapshot()
    assert not any(n.startswith(".snapshot-partial") for n in w.listing())


def test_the_pure_rules():
    assert [rules.candidate("snapshot-20261001T080000.db", n) for n in (1, 2, 3)] == [
        "snapshot-20261001T080000.db", "snapshot-20261001T080000-2.db", "snapshot-20261001T080000-3.db"]
    names = ["snapshot-20261001T080000-10.db", "snapshot-20261001T080000-9.db", "snapshot-20261001T080000.db", "x.db"]
    assert rules.newest_first(names) == names[:3]                                    # -10 sorts after -9, not before
    assert rules.new_violations(["a", "b"], ["b", "c"]) == ["c"]


# ---------------------------------------------------------------- restore to a new file

def test_restore_copies_a_snapshot_to_a_new_file(w, tmp_path):
    snap = w.snapshots().take_snapshot().path
    into = str(tmp_path / "restored.db")
    assert w.snapshots().restore_snapshot(snap, into) >= 1
    assert w.rows("SELECT statement FROM facts ORDER BY id", db=into) == w.rows("SELECT statement FROM facts ORDER BY id")
    assert w.rows("PRAGMA integrity_check", db=into) == [("ok",)]


def test_restore_never_overwrites_and_refuses_a_bad_source(w, tmp_path):
    snap = w.snapshots().take_snapshot().path
    into = str(tmp_path / "restored.db")
    open(into, "w").write("precious")
    with pytest.raises(FileExistsError):
        w.snapshots().restore_snapshot(snap, into)
    assert open(into).read() == "precious"
    bad = str(tmp_path / "garbage.db")
    open(bad, "wb").write(b"this is not a database" * 100)
    fresh = str(tmp_path / "fresh.db")
    with pytest.raises(rules.SnapshotError):
        w.snapshots().restore_snapshot(bad, fresh)
    assert not os.path.exists(fresh)
    with pytest.raises(rules.SnapshotError, match="no database"):
        w.snapshots().restore_snapshot(str(tmp_path / "missing.db"), fresh)


# ---------------------------------------------------------------- restore-tables

def damage(w):
    w.sql("DELETE FROM artists")
    w.sql("UPDATE facts SET statement = 'Vandalised' WHERE id = 1")


def test_restore_tables_from_a_snapshot_replaces_rows_and_takes_a_safety_snapshot_first(w):
    snap = w.snapshots().take_snapshot().path
    damage(w)
    result = w.restorer(snapshot=snap).restore_tables_from_snapshot(["artists", "facts"])
    assert result.replaced == {"artists": 2, "facts": 2} and result.committed
    assert w.rows("SELECT name FROM artists ORDER BY id") == [("Some Band",), ("Other Act",)]
    assert w.rows("SELECT statement FROM facts WHERE id = 1") == [("Plain.",)]
    assert w.rows("SELECT COUNT(*) FROM facts_fts WHERE facts_fts MATCH 'vandalised'") == [(0,)]       # the index follows
    safety = result.safety_snapshot
    assert os.path.dirname(safety) == w.snaps and safety != snap
    assert w.rows("SELECT statement FROM facts WHERE id = 1", db=safety) == [("Vandalised",)]          # the state it replaced


def test_restore_tables_from_an_export_folder(w):
    w.export()
    damage(w)
    result = w.restorer(export=w.export_dir).restore_tables_from_export(["artists"])
    assert result.replaced == {"artists": 2}
    assert w.rows("SELECT name FROM artists ORDER BY id") == [("Some Band",), ("Other Act",)]
    assert w.rows("SELECT statement FROM facts WHERE id = 1") == [("Vandalised",)]        # facts were not named: untouched


def test_a_dry_run_changes_nothing_and_takes_no_snapshot(w):
    snap = w.snapshots().take_snapshot().path
    damage(w)
    before = open(w.db, "rb").read()
    result = w.restorer(snapshot=snap).restore_tables_from_snapshot(["artists"], dry_run=True)
    assert result.replaced == {"artists": 2} and not result.committed and result.safety_snapshot == ""
    assert w.rows("SELECT COUNT(*) FROM artists") == [(0,)]
    assert open(w.db, "rb").read() == before
    assert w.listing() == [os.path.basename(snap)]


@pytest.mark.parametrize("table", ["fact_revisions", "audit_log", "operation_log"])
def test_history_tables_are_never_replaced_in_place(w, table):
    snap = w.snapshots().take_snapshot().path
    with pytest.raises(rules.SnapshotError, match="history"):
        w.restorer(snapshot=snap).restore_tables_from_snapshot([table])
    w.export()
    with pytest.raises(rules.SnapshotError, match="history"):
        w.restorer(export=w.export_dir).restore_tables_from_export([table])


@pytest.mark.parametrize("tables,message", [([], "at least one"), (["facts; DROP TABLE facts"], "valid table name"),
                                            (["Facts"], "valid table name"), (["build_info"], "not restorable")])
def test_bad_table_names_are_refused(w, tables, message):
    snap = w.snapshots().take_snapshot().path
    with pytest.raises(rules.SnapshotError, match=message):
        w.restorer(snapshot=snap).restore_tables_from_snapshot(tables)


def test_a_restore_that_would_leave_dangling_references_changes_nothing(w):
    snap = w.snapshots().take_snapshot().path
    w.sql("DELETE FROM subjects WHERE id = 2")                      # the snapshot has a subject the facts point at
    w.sql("UPDATE facts SET subject_id = 1")
    w.sql("UPDATE facts SET subject_id = 2 WHERE id = 2", db=snap)  # make the snapshot's facts need subject 2
    w.sql("DELETE FROM subjects WHERE id = 2", db=snap)             # ... but its subjects table lacks it
    before = w.rows("SELECT id, subject_id FROM facts ORDER BY id")
    with pytest.raises(rules.SnapshotError, match="dangling"):
        w.restorer(snapshot=snap).restore_tables_from_snapshot(["facts"])
    assert w.rows("SELECT id, subject_id FROM facts ORDER BY id") == before


def test_a_failure_in_the_second_table_rolls_back_the_first(w):
    snap = w.snapshots().take_snapshot().path
    damage(w)
    w.sql("ALTER TABLE facts RENAME COLUMN notes TO annotations")   # the live schema no longer matches the snapshot's facts
    with pytest.raises(rules.SnapshotError, match="lacks"):
        w.restorer(snapshot=snap).restore_tables_from_snapshot(["artists", "facts"])
    assert w.rows("SELECT COUNT(*) FROM artists") == [(0,)]          # artists was not restored either


def test_dangling_references_that_were_already_there_do_not_block_an_unrelated_restore(w):
    snap = w.snapshots().take_snapshot().path
    w.sql("INSERT INTO fact_sources (fact_id, source_id) VALUES (1, 77)")        # already dangling before the restore
    damage(w)
    result = w.restorer(snapshot=snap).restore_tables_from_snapshot(["artists"])
    assert result.committed and w.rows("SELECT COUNT(*) FROM artists") == [(2,)]


def test_schema_version_and_missing_table_mismatches_are_refused(w):
    snap = w.snapshots().take_snapshot().path
    con = sqlite3.connect(snap)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    with pytest.raises(rules.SnapshotError, match="schema version"):
        w.restorer(snapshot=snap).restore_tables_from_snapshot(["artists"])
    con = sqlite3.connect(snap)
    con.execute("PRAGMA user_version = %d" % w.rows("PRAGMA user_version")[0][0])
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("DROP TABLE claim_facts")
    con.commit()
    con.close()
    with pytest.raises(rules.SnapshotError, match="lacks table"):
        w.restorer(snapshot=snap).restore_tables_from_snapshot(["claim_facts"])


def test_a_forged_or_incomplete_export_is_refused(w):
    w.export(tables=("artists",))
    path = os.path.join(w.export_dir, "bulk", "artists.ndjson")
    open(path, "w").write(open(path).read().replace("Some Band", "Forged"))
    with pytest.raises(rules.SnapshotError, match="damaged"):
        w.restorer(export=w.export_dir).restore_tables_from_export(["artists"])
    with pytest.raises(rules.SnapshotError, match="does not hold"):
        w.restorer(export=w.export_dir).restore_tables_from_export(["measurements"])
    with pytest.raises(export_rules.ExportError, match="missing"):
        w.restorer(export=w.export_dir).restore_tables_from_export(["facts"])


def test_a_restore_is_audited_as_a_restore_and_clears_the_context(w):
    con = sqlite3.connect(w.db)
    live_schema_store.install(con)
    con.close()
    snap = w.snapshots().take_snapshot().path
    damage(w)
    w.restorer(snapshot=snap).restore_tables_from_snapshot(["facts"])
    vias = w.rows("SELECT DISTINCT via FROM audit_log WHERE table_name = 'facts' AND op IN ('delete', 'insert') AND id > "
                  "(SELECT MAX(id) FROM audit_log WHERE new_json LIKE '%Vandalised%')")
    assert vias == [("restore",)]
    assert w.rows("SELECT COUNT(*) FROM audit_context") == [(0,)]


def test_two_restores_cannot_overlap(w):
    snap = w.snapshots().take_snapshot().path
    blocker = sqlite3.connect(w.db, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")                       # someone else is writing
    try:
        with pytest.raises(rules.SnapshotError, match="nothing was changed"):
            w.restorer(snapshot=snap).restore_tables_from_snapshot(["artists"])
    finally:
        blocker.rollback()
        blocker.close()


def test_a_database_error_during_a_restore_is_reported_as_an_error_line_not_a_traceback(w):
    snap = w.snapshots().take_snapshot().path
    blocker = sqlite3.connect(w.db, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        r = cli(w, "restore-tables", "--db", w.db, "--snapshot-dir", w.snaps, "--from-snapshot", snap, "--table", "artists")
    finally:
        blocker.rollback()
        blocker.close()
    assert r.returncode == 1 and r.stderr.startswith("error: ") and "Traceback" not in r.stderr, r.stderr


def test_an_unwritable_snapshot_directory_is_reported_as_an_error_line(w, tmp_path):
    blocked = tmp_path / "a-file-not-a-folder"
    blocked.write_text("x")
    r = cli(w, "snapshot", "--db", w.db, "--dir", str(blocked / "inside"))
    assert r.returncode == 1 and r.stderr.startswith("error: ") and "Traceback" not in r.stderr, r.stderr


# ---------------------------------------------------------------- the command line

def cli(w, *args):
    config = write_config(os.path.join(w.root, "config.toml"), w.root, os.path.join(w.root, "data"))
    return subprocess.run([sys.executable, KNOWLEDGE, "db", *args], capture_output=True, text=True, cwd=w.root,
                          env={**os.environ, "KNOWLEDGE_CONFIG": config, "KNOWLEDGE_FROZEN_NOW": T1})


def test_the_commands_snapshot_list_restore_and_restore_tables(w, tmp_path):
    r = cli(w, "snapshot", "--db", w.db, "--dir", w.snaps)
    assert r.returncode == 0 and r.stdout.strip().endswith("snapshot-20261001T080000.db"), r.stderr
    snap = r.stdout.strip()
    assert cli(w, "snapshots", "--dir", w.snaps).stdout.split() == ["snapshot-20261001T080000.db"]
    into = str(tmp_path / "back.db")
    r = cli(w, "restore", "--from", snap, "--into", into)
    assert r.returncode == 0 and "restored" in r.stdout, r.stderr
    assert cli(w, "restore", "--from", snap, "--into", into).returncode == 1
    damage(w)
    r = cli(w, "restore-tables", "--db", w.db, "--table", "artists", "--from-snapshot", snap, "--snapshot-dir", w.snaps, "--dry-run")
    assert r.returncode == 0 and "artists: 2" in r.stdout and "nothing changed" in r.stdout, r.stderr
    r = cli(w, "restore-tables", "--db", w.db, "--table", "artists", "--from-snapshot", snap, "--snapshot-dir", w.snaps)
    assert r.returncode == 0 and "safety snapshot:" in r.stdout, r.stderr
    assert w.rows("SELECT COUNT(*) FROM artists") == [(2,)]


def test_restore_tables_needs_exactly_one_source_and_reports_refusals(w):
    r = cli(w, "restore-tables", "--db", w.db, "--table", "artists")
    assert r.returncode == 1 and "exactly one source" in r.stderr
    r = cli(w, "restore-tables", "--db", w.db, "--table", "fact_revisions", "--from-snapshot", w.db)
    assert r.returncode == 1 and "history" in r.stderr
    assert cli(w, "snapshot", "--db", w.db, "--dir", w.snaps, "--keep", "0").returncode == 1


# ---- the safety snapshot of a restore is withdrawn when the restore is refused, and counts only when it goes through

def _dangling_setup(w):
    snap = w.snapshots().take_snapshot().path
    w.sql("DELETE FROM subjects WHERE id = 2")
    w.sql("UPDATE facts SET subject_id = 1")
    w.sql("UPDATE facts SET subject_id = 2 WHERE id = 2", db=snap)
    w.sql("DELETE FROM subjects WHERE id = 2", db=snap)
    return snap


def test_refused_restores_leave_every_retained_snapshot_untouched(w):
    snap = _dangling_setup(w)
    for i in range(1, 4):
        w.snapshots(clock=f"2026-10-01T08:00:0{i}+00:00").take_snapshot()
    kept = w.listing()
    for _ in range(rules.DEFAULT_KEEP + 2):                          # more refused attempts than the retention limit
        with pytest.raises(rules.SnapshotError, match="dangling"):
            w.restorer(snapshot=snap).restore_tables_from_snapshot(["facts"])
    assert w.listing() == kept, "a refused restore must not add a snapshot, nor prune an older one"


def test_a_restore_that_fails_while_staging_leaves_no_safety_snapshot(w):
    snap = w.snapshots().take_snapshot().path
    w.sql("ALTER TABLE facts RENAME COLUMN notes TO annotations")
    kept = w.listing()
    with pytest.raises(rules.SnapshotError, match="lacks"):
        w.restorer(snapshot=snap).restore_tables_from_snapshot(["facts"])
    assert w.listing() == kept


def test_a_restore_that_goes_through_keeps_its_safety_snapshot_and_respects_the_retention_limit(w):
    for i in range(rules.DEFAULT_KEEP):
        w.snapshots(clock=f"2026-10-01T08:00:{i:02d}+00:00").take_snapshot()
    oldest = w.listing()[0]
    snap = os.path.join(w.snaps, w.listing()[-1])
    damage(w)
    result = w.restorer(snapshot=snap).restore_tables_from_snapshot(["artists"])
    listing = w.listing()
    assert result.committed and os.path.basename(result.safety_snapshot) in listing
    assert len(listing) == rules.DEFAULT_KEEP and oldest not in listing        # one in, the oldest out
