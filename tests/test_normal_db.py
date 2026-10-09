"""#21: knowledge-normal.db is built by copying a whitelist INTO a fresh db, then leak-checked.
Fixture data (built from the real schema.sql by leak_test.build_fixture) holds known private markers."""
import importlib
import os
import shutil
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import build  # noqa: E402
import leak_test  # noqa: E402
import normal_db  # noqa: E402
import privacy  # noqa: E402

M = leak_test.MARKERS
NON_FACT_TABLES = ["claims", "claim_facts", "vault_files", "metrics", "measurements", "fact_measurements", "exercises",
                   "training_sets", "foods", "food_log_entries", "meal_log_entries", "muscles", "muscle_volume_weekly",
                   "import_sources", "artists", "artist_members", "venues", "festivals", "concert_attendances",
                   "albums", "tracks", "scrobbles"]


def rules():
    return privacy.Rules(subject_tags={M["private subject name"]: "private"},
                         keywords=(M["rules-file name in untagged subject"].split()[0].lower(),))


@pytest.fixture
def built(tmp_path):
    full = leak_test.build_fixture(str(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    path, counts = normal_db.build_normal_atomic(full, str(out), rules())
    return type("B", (), dict(full=full, out=out, path=path, counts=counts, tmp=tmp_path))


def q(path, sql, *a):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, a).fetchall()
    finally:
        con.close()


def test_leak_self_test_passes_and_proves_it_can_fail(tmp_path):
    assert leak_test.self_test(str(tmp_path), verbose=False) == []


def test_no_marker_in_rows_or_raw_bytes(built):
    assert leak_test.scan(built.path, M) == []
    assert leak_test.scan_against_full(built.path, built.full) == []
    raw = open(built.path, "rb").read()
    for marker in M.values():
        assert marker.encode().lower() not in raw.lower()
    for stray in (b"sess-secret-1", b"because reasons", b"Secret Band"):
        assert stray not in raw


def test_scan_flags_an_injected_marker_in_a_neighbor_row(built):
    con = sqlite3.connect(built.path)
    con.execute("UPDATE sources SET description = ? WHERE id = 1", (f"cites {M['claim text']}",))
    con.commit()
    con.close()
    leaks = leak_test.scan(built.path, {"claim": M["claim text"]})
    assert any("sources.description" in w for _l, _m, w in leaks) and any(w == "raw file bytes" for _l, _m, w in leaks)


def test_scan_flags_text_left_in_free_pages_not_in_any_row(built):
    # the delete-from-a-copy design leaves text in the file; the raw-bytes check must see it
    con = sqlite3.connect(built.path)
    con.execute("CREATE TABLE junk (t TEXT)")
    con.execute("INSERT INTO junk VALUES (?)", (M["claim text"],))
    con.commit()
    con.execute("DROP TABLE junk")
    con.commit()
    con.close()
    assert [w for _l, _m, w in leak_test.scan(built.path, {"c": M["claim text"]})] == ["raw file bytes"]


def test_scan_refuses_empty_marker(built):
    with pytest.raises(ValueError):
        leak_test.scan(built.path, [" "])


def test_only_whitelisted_tables_exist_and_non_fact_tables_are_absent(built):
    names = {r[0] for r in q(built.path, "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    for t in NON_FACT_TABLES:
        assert t not in names
    assert set(normal_db.TABLES) <= names
    assert not q(built.path, "SELECT 1 FROM sqlite_master WHERE type='trigger'")
    con = sqlite3.connect(built.path)
    normal_db.check_structure(con)


def test_check_structure_rejects_an_extra_table(built):
    con = sqlite3.connect(built.path)
    con.execute("CREATE TABLE claims (id INTEGER)")
    with pytest.raises(normal_db.NormalDbError):
        normal_db.check_structure(con)


def test_expected_rows(built):
    assert [r[0] for r in q(built.path, "SELECT statement FROM facts ORDER BY id")] == list(leak_test.NORMAL_STATEMENTS)
    assert built.counts["facts"] == 2
    # fact 5 is stored 'normal' but sits under a private subject: the floor keeps it out
    assert M["private-subject fact under normal-looking fact"] not in str(q(built.path, "SELECT statement FROM facts"))
    cols = {r[1] for r in q(built.path, "PRAGMA table_info(facts)")}
    assert not cols & {"is_personal", "notes", "origin_file_id", "source_quote", "session_id", "captured_via", "provided_by"}
    assert {r[1] for r in q(built.path, "PRAGMA table_info(sources)")}.isdisjoint({"origin_path", "created_at"})


def test_subjects_are_only_those_with_normal_facts_plus_ancestors_with_normal_counts(built):
    assert {r[0] for r in q(built.path, "SELECT name FROM subjects")} == {"nutrition", "sleep"}
    # nutrition holds private facts 3 and 7 too; its count must be normal facts only
    assert q(built.path, "SELECT name, fact_count FROM v_subjects ORDER BY name") == [("nutrition", 1), ("sleep", 1)]
    assert not any(c[1] == "fact_count" for c in q(built.path, "PRAGMA table_info(subjects)"))


def test_shared_source_included_but_private_facts_quote_is_not(built):
    assert q(built.path, "SELECT fact_id, source_id, locator, quote FROM fact_sources") == [
        (1, 1, "p. 4", "public quote about creatine")]
    assert [r[0] for r in q(built.path, "SELECT name FROM sources")] == ["A shared study"]
    assert q(built.path, "SELECT name FROM publishers") == [("Journal of Fixtures",)]
    assert q(built.path, "SELECT name FROM authors") == [("A. Public",)]


def test_revision_history_rules(built):
    # fact 2: older revision normal, a newer one private -> only the normal ones are copied
    assert q(built.path, "SELECT fact_id, revision FROM fact_revisions ORDER BY fact_id, revision") == [(1, 1), (1, 2), (2, 1)]
    assert not q(built.path, "SELECT 1 FROM fact_revisions WHERE statement LIKE '%Xylophone%'")
    assert {r[1] for r in q(built.path, "PRAGMA table_info(fact_revisions)")}.isdisjoint({"session_id", "change_reason", "notes"})


def test_fts_is_rebuilt_over_normal_rows_only(built):
    assert [r[0] for r in q(built.path, "SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'creatine'")] == [1]
    assert not q(built.path, "SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'Velthorn'")
    assert q(built.path, "SELECT rowid FROM sources_fts WHERE sources_fts MATCH 'shared'") == [(1,)]


def test_unicode_survives(built):
    assert q(built.path, "SELECT 1 FROM facts WHERE statement LIKE '%café%'")


def test_integrity_and_foreign_keys(built):
    assert q(built.path, "PRAGMA integrity_check") == [("ok",)]
    assert q(built.path, "PRAGMA foreign_key_check") == []


def _full(tmp_path, populate=None):
    full = leak_test.build_fixture(str(tmp_path))
    if populate:
        con = sqlite3.connect(full)
        populate(con)
        con.commit()
        con.close()
    return full


def test_no_normal_facts_gives_an_empty_but_valid_db(tmp_path):
    full = _full(tmp_path, lambda c: c.execute("UPDATE facts SET visibility = 'private'"))
    path, counts = normal_db.build_normal_atomic(full, str(tmp_path), rules())
    assert set(counts) == set(normal_db.TABLES)
    assert {t: n for t, n in counts.items() if n} == {"build_info": 1}        # #47: the build stamp is not a fact
    assert q(path, "SELECT * FROM v_subjects") == []
    assert q(path, "PRAGMA integrity_check") == [("ok",)]


def test_empty_full_db_with_no_fact_rows(tmp_path):
    def p(c):
        for t in ("fact_revisions", "fact_sources", "claim_facts", "facts"):
            c.execute(f"DELETE FROM {t}")
    full = _full(tmp_path, p)
    path, counts = normal_db.build_normal_atomic(full, str(tmp_path), rules())
    assert counts["facts"] == 0


def test_rule_added_after_ingest_keeps_a_stored_normal_fact_out(tmp_path):
    full = _full(tmp_path)
    path, counts = normal_db.build_normal_atomic(full, str(tmp_path), privacy.Rules(keywords=("creatine",)))
    assert [x[0] for x in q(path, "SELECT id FROM facts")] == [2]


def test_old_normal_revision_with_a_now_listed_name_is_dropped(tmp_path):
    def p(c):
        c.execute("INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement, trust_level, status, visibility) "
                  "VALUES (2, 'k-normal-2', 3, '2026-03-01T00:00:00+00:00', 'cli', 'Harmless? No: Wibblesnort stayed over', 'high', 'active', 'normal')")
    full = _full(tmp_path, p)
    path, _ = normal_db.build_normal_atomic(full, str(tmp_path), privacy.Rules(keywords=("wibblesnort",)))
    assert not q(path, "SELECT 1 FROM fact_revisions WHERE statement LIKE '%Wibblesnort%'")
    assert leak_test.scan(path, ["Wibblesnort"]) == []


def test_superseded_by_a_private_fact_is_nulled(tmp_path):
    def p(c):
        c.execute("UPDATE facts SET superseded_by_fact_id = 3, status = 'superseded' WHERE id = 1")
        c.execute("UPDATE fact_revisions SET superseded_by = 'k-priv-1' WHERE fact_id = 1")
    full = _full(tmp_path, p)
    path, _ = normal_db.build_normal_atomic(full, str(tmp_path), rules())
    assert q(path, "SELECT superseded_by_fact_id FROM facts WHERE id = 1") == [(None,)]
    assert q(path, "SELECT DISTINCT superseded_by FROM fact_revisions WHERE fact_id = 1") == [(None,)]


def test_subject_loop_fails_closed(tmp_path):
    full = _full(tmp_path, lambda c: c.execute("UPDATE subjects SET parent_id = 2 WHERE id = 1"))  # nutrition <-> sleep
    path, counts = normal_db.build_normal_atomic(full, str(tmp_path), rules())
    assert counts["facts"] == 0


def test_replaces_a_stale_target_atomically(tmp_path):
    full = _full(tmp_path)
    final = tmp_path / normal_db.NORMAL_DB_NAME
    final.write_bytes(b"old " + M["claim text"].encode())
    normal_db.build_normal_atomic(full, str(tmp_path), rules())
    assert M["claim text"].encode() not in final.read_bytes()
    assert [p.name for p in tmp_path.iterdir() if "building" in p.name] == []


def test_failed_build_removes_temp_and_stale_normal_db(tmp_path):
    def p(c):
        # a private marker in a source name cited by a normal fact: the derived-marker leak check fails the build
        c.execute("UPDATE sources SET name = ? WHERE id = 1", (M["claim text"],))
    full = _full(tmp_path, p)
    final = tmp_path / normal_db.NORMAL_DB_NAME
    final.write_bytes(b"stale")
    with pytest.raises(normal_db.NormalDbError):
        normal_db.build_normal_atomic(full, str(tmp_path), rules())
    assert not final.exists()
    assert [p.name for p in tmp_path.iterdir() if "building" in p.name] == []


def test_unwritable_directory_raises_and_leaves_nothing(tmp_path):
    full = _full(tmp_path)
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        if os.access(ro, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        with pytest.raises(OSError):
            normal_db.build_normal_atomic(full, str(ro), rules())
    finally:
        os.chmod(ro, 0o700)
    assert list(ro.iterdir()) == []


def test_full_db_is_opened_read_only(tmp_path):
    full = _full(tmp_path)
    before = open(full, "rb").read()
    normal_db.build_normal_atomic(full, str(tmp_path), rules())
    assert open(full, "rb").read() == before


def test_cli_leak_test_real_mode_flags_a_leak(built, capsys):
    assert leak_test.main(["--full", built.full, "--normal", built.path]) == 0
    con = sqlite3.connect(built.path)
    con.execute("UPDATE facts SET trust_rationale = ? WHERE id = 1", (M["private fact text"],))
    con.commit()
    con.close()
    assert leak_test.main(["--full", built.full, "--normal", built.path]) == 1
    assert "LEAK" in capsys.readouterr().err


# ---- the build.py hook -------------------------------------------------------

@pytest.fixture
def hooked(tmp_path, monkeypatch):
    """build.main() with a fake pipeline that makes the fixture full DB, so the real hook runs."""
    db_dir = tmp_path / "db"
    fixture = leak_test.build_fixture(str(tmp_path))
    monkeypatch.setattr(build, "build", lambda target: shutil.copy(fixture, target))
    monkeypatch.setattr(build, "report", lambda p: None)
    monkeypatch.setattr(build, "DB_DIR", str(db_dir))
    monkeypatch.setattr(build, "LIVE_DB", str(db_dir / "knowledge.db"))
    monkeypatch.setattr(build, "BACKUP_DIR", str(db_dir / "backups"))
    monkeypatch.setattr(importlib.import_module("privacy_store"), "load_rules", lambda path=None: rules())
    return db_dir


def run_main(*args):
    sys.argv = ["build.py", *args]
    build.main()


def test_build_writes_both_dbs_into_the_same_dir(hooked, capsys):
    run_main()
    assert sorted(p.name for p in hooked.iterdir() if p.is_file()) == ["knowledge-normal.db", "knowledge.db"]
    assert leak_test.scan(str(hooked / "knowledge-normal.db"), M) == []
    assert "facts: 2" in capsys.readouterr().out


def test_normal_failure_exits_nonzero_but_keeps_the_main_build(hooked, monkeypatch, capsys):
    run_main()

    def boom(*a, **k):
        raise normal_db.NormalDbError("kaboom")
    monkeypatch.setattr(normal_db, "build_normal_atomic", boom)
    with pytest.raises(SystemExit) as ei:
        run_main()
    assert ei.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "normal-only DB failed" in err and "kaboom" in err
    # the second main build was swapped in, intact (and the first one backed up)
    assert q(str(hooked / "knowledge.db"), "PRAGMA integrity_check") == [("ok",)]
    assert q(str(hooked / "knowledge.db"), "SELECT COUNT(*) FROM facts") == [(7,)]
    assert len(list((hooked / "backups").iterdir())) == 1


def test_bad_rules_file_fails_the_normal_build_closed(hooked, monkeypatch, capsys):
    def bad(path=None):
        raise privacy.PrivacyRulesError("corrupt rules")
    monkeypatch.setattr(importlib.import_module("privacy_store"), "load_rules", bad)
    with pytest.raises(SystemExit):
        run_main()
    assert (hooked / "knowledge.db").exists() and not (hooked / "knowledge-normal.db").exists()
    assert "corrupt rules" in capsys.readouterr().err


def test_check_builds_a_normal_db_in_a_temp_dir_only(hooked, capsys):
    run_main("--check")
    assert not hooked.exists()
    assert "Normal-only DB" in capsys.readouterr().out
