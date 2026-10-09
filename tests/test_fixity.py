"""#38: file fixity (fingerprint), ingest of the fixity columns, add_fact's baseline, the backfill
tool and the changed-since-extraction audit. Temp dirs and in-memory dbs only."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

import fixity_store
from test_add_fact import REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)

@pytest.fixture(autouse=True)
def _restore_modules():
    """These tests import add_fact / backfill_* lazily; leave sys.modules as found so other tests'
    module-identity assumptions (isinstance on privacy classes) are not disturbed."""
    before = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


def make_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO subjects (id, name, domain) VALUES (1, 's', 'general')")
    return con


def add_fact(con, fid, path, baseline):
    """A fact whose origin file is `path` with the given baseline hash (None = no baseline)."""
    row = con.execute("SELECT id FROM vault_files WHERE path = ?", (path,)).fetchone()
    vid = row[0] if row else con.execute("INSERT INTO vault_files (path) VALUES (?)", (path,)).lastrowid
    con.execute("INSERT INTO facts (id, subject_id, statement, trust_level, source_key, freshness, origin_file_id, "
                "extracted_from_sha256) VALUES (?, 1, ?, 'low', ?, 'unreviewed', ?, ?)",
                (fid, f"fact {fid}", f"k-{fid}", vid, baseline))


# ------------------------------------------------------------------ fingerprint

def test_fingerprint_of_a_present_file(tmp_path):
    p = write(tmp_path / "note.md", b"hello\n")
    fp = fixity_store.fingerprint(p)
    assert fp["content_sha256"] == sha(b"hello\n") and fp["size_bytes"] == 6
    assert fp["mime_type"] == "text/markdown" and fp["file_state"] == "present"
    assert fp["file_mtime"].endswith("+00:00") and "." not in fp["file_mtime"]


def test_empty_file_has_the_empty_hash_and_is_present(tmp_path):
    fp = fixity_store.fingerprint(write(tmp_path / "e.md", b""))
    assert (fp["content_sha256"], fp["size_bytes"], fp["file_state"]) == (EMPTY_SHA, 0, "present")


def test_file_larger_than_the_read_buffer_hashes_correctly(tmp_path, monkeypatch):
    data = os.urandom(5000)
    p = write(tmp_path / "big.md", data)
    monkeypatch.setattr(fixity_store, "HASH_BUFFER", 64)  # many chunks, last one partial
    assert fixity_store.fingerprint(p)["content_sha256"] == sha(data)


@pytest.mark.parametrize("make", [lambda t: str(t / "nope.md"), lambda t: str(t), lambda t: "", lambda t: None])
def test_missing_directory_blank_and_none_paths_are_missing_without_a_hash(tmp_path, make):
    fp = fixity_store.fingerprint(make(tmp_path))
    assert fp["file_state"] == "missing"
    assert (fp["content_sha256"], fp["size_bytes"], fp["file_mtime"]) == (None, None, None)


def test_unreadable_file_is_missing_not_a_crash(tmp_path):
    p = write(tmp_path / "locked.md", b"x")
    os.chmod(p, 0)
    try:
        if os.access(p, os.R_OK):
            pytest.skip("running as a user that can read mode-000 files")
        assert fixity_store.fingerprint(p)["file_state"] == "missing"
    finally:
        os.chmod(p, 0o600)


def test_tilde_paths_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    write(tmp_path / "v" / "n.md", b"abc")
    assert fixity_store.fingerprint("~/v/n.md")["content_sha256"] == sha(b"abc")


def test_valid_sha256():
    assert fixity_store.valid_sha256(EMPTY_SHA)
    for bad in (None, "", "abc", EMPTY_SHA.upper(), EMPTY_SHA + "0", 5):
        assert not fixity_store.valid_sha256(bad)


# ------------------------------------------------------------------ audit

def test_audit_unchanged_file_has_no_row(tmp_path):
    con = make_db()
    p = write(tmp_path / "a.md", b"one")
    add_fact(con, 1, p, sha(b"one"))
    assert fixity_store.audit_sources(con) == []


def test_audit_edited_file_is_a_changed_row(tmp_path):
    con = make_db()
    p = write(tmp_path / "a.md", b"one")
    add_fact(con, 1, p, sha(b"one"))
    write(p, b"one, edited")
    (row,) = fixity_store.audit_sources(con)
    assert (row["fact_id"], row["reason"], row["path"]) == (1, "changed", p)
    assert row["baseline_sha256"] == sha(b"one") and row["current_sha256"] == sha(b"one, edited")
    assert row["moved_to"] is None and row["source_key"] == "k-1"


def test_audit_deleted_file_is_missing(tmp_path):
    con = make_db()
    p = write(tmp_path / "a.md", b"one")
    add_fact(con, 1, p, sha(b"one"))
    os.remove(p)
    (row,) = fixity_store.audit_sources(con)
    assert (row["reason"], row["current_sha256"], row["moved_to"]) == ("missing", None, None)


def test_audit_renamed_file_found_under_a_search_root_is_moved(tmp_path):
    con = make_db()
    old = write(tmp_path / "vault" / "a.md", b"one")
    add_fact(con, 1, old, sha(b"one"))
    new = str(tmp_path / "vault" / "sub" / "renamed.md")
    os.makedirs(os.path.dirname(new))
    os.rename(old, new)
    (row,) = fixity_store.audit_sources(con, search_roots=[str(tmp_path / "vault")])
    assert (row["reason"], row["moved_to"]) == ("moved", new)
    # without a place to look, a disappeared path stays "missing"
    assert fixity_store.audit_sources(con)[0]["reason"] == "missing"


def test_audit_moved_to_another_known_vault_file(tmp_path):
    con = make_db()
    old = write(tmp_path / "a.md", b"one")
    add_fact(con, 1, old, sha(b"one"))
    other = write(tmp_path / "b.md", b"x")
    add_fact(con, 2, other, sha(b"x"))
    os.rename(old, str(tmp_path / "c.md"))
    con.execute("INSERT INTO vault_files (path) VALUES (?)", (str(tmp_path / "c.md"),))
    (row,) = fixity_store.audit_sources(con)
    assert (row["fact_id"], row["reason"], row["moved_to"]) == (1, "moved", str(tmp_path / "c.md"))


def test_fact_with_no_baseline_is_reported_separately_not_as_a_change(tmp_path):
    con = make_db()
    p = write(tmp_path / "a.md", b"one")
    add_fact(con, 1, p, None)
    write(p, b"edited")
    assert fixity_store.audit_sources(con) == []
    assert fixity_store.unbaselined_facts(con) == [{"fact_id": 1, "source_key": "k-1", "path": p}]


def test_audit_empty_file_baseline_and_large_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fixity_store, "HASH_BUFFER", 16)
    con = make_db()
    e = write(tmp_path / "e.md", b"")
    big_data = b"y" * 1000
    b = write(tmp_path / "b.md", big_data)
    add_fact(con, 1, e, EMPTY_SHA)
    add_fact(con, 2, b, sha(big_data))
    assert fixity_store.audit_sources(con) == []
    write(b, big_data + b"!")
    assert [r["fact_id"] for r in fixity_store.audit_sources(con)] == [2]
    write(e, b"now not empty")
    assert [r["fact_id"] for r in fixity_store.audit_sources(con)] == [1, 2]


def test_audit_never_writes(tmp_path):
    con = make_db()
    p = write(tmp_path / "a.md", b"one")
    add_fact(con, 1, p, sha(b"one"))
    write(p, b"two")
    before = con.total_changes
    fixity_store.audit_sources(con)
    assert con.total_changes == before


# ------------------------------------------------------------------ ingest

def test_vault_file_row_is_filled_and_counts_are_unchanged(ingest, tmp_path):
    p = write(tmp_path / "v" / "n.md", b"note")
    con = ingest.run04([F(origin_path=p), F(statement="Second from the same file.", origin_path=p),
                        F(statement="Gone.", origin_path=str(tmp_path / "v" / "gone.md"))])
    assert con.execute("SELECT COUNT(*) FROM vault_files").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 3
    r = con.execute("SELECT * FROM vault_files WHERE path = ?", (p,)).fetchone()
    assert (r["content_sha256"], r["size_bytes"], r["mime_type"], r["file_state"]) == (sha(b"note"), 4, "text/markdown", "present")
    g = con.execute("SELECT * FROM vault_files WHERE path LIKE '%gone.md'").fetchone()
    assert (g["content_sha256"], g["size_bytes"], g["file_state"]) == (None, None, "missing")


def test_04_and_11_store_the_baseline_and_reject_a_malformed_one(ingest):
    good = sha(b"x")
    con = ingest.run04([F(extracted_from_sha256=good), F(statement="No baseline.")])
    assert [r[0] for r in con.execute("SELECT extracted_from_sha256 FROM facts ORDER BY id")] == [good, None]
    con = ingest.run11([F(extracted_from_sha256=good)])
    assert con.execute("SELECT extracted_from_sha256 FROM facts").fetchone()[0] == good
    for bad in ("", "xyz", good.upper()):
        with pytest.raises(ValueError, match="extracted_from_sha256"):
            ingest.run04([F(extracted_from_sha256=bad)])
        with pytest.raises(ValueError, match="extracted_from_sha256"):
            ingest.run11([F(extracted_from_sha256=bad)])


def test_11_links_an_origin_path(ingest, tmp_path):
    p = write(tmp_path / "o.md", b"o")
    con = ingest.run11([F(origin_path=p)])
    assert con.execute("SELECT vf.path FROM facts f JOIN vault_files vf ON vf.id = f.origin_file_id").fetchone()[0] == p


def test_02_fills_the_source_fixity_columns(ingest, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ing_02", os.path.join(REPO, "ingest", "literature_sources.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path / "sources"
    write(d / "smith2020.md", b"---\ntitle: T\nyear: 2020\n---\nbody\n")
    write(d / "README.md", b"skipped")
    monkeypatch.setattr(mod, "SRC_DIR", str(d))
    con = ingest.db()
    mod.run(con)
    r = con.execute("SELECT * FROM sources").fetchone()
    assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert r["content_sha256"] == sha(b"---\ntitle: T\nyear: 2020\n---\nbody\n")
    assert (r["mime_type"], r["file_state"]) == ("text/markdown", "present")


# ------------------------------------------------------------------ add_fact

def test_add_fact_records_the_baseline_when_the_origin_file_is_readable(tmp_path):
    import add_fact
    p = write(tmp_path / "o.md", b"origin")
    entry = add_fact.build_entry(add_fact.NewFact(statement="s", subject="x", trust_level="low", no_decay=True,
                                                  recheck_rationale="r", origin_path=p))
    assert entry["origin_path"] == p and entry["extracted_from_sha256"] == sha(b"origin")


def test_add_fact_unreadable_or_absent_origin_records_no_hash(tmp_path):
    import add_fact
    kw = dict(statement="s", subject="x", trust_level="low", no_decay=True, recheck_rationale="r")
    entry = add_fact.build_entry(add_fact.NewFact(origin_path=str(tmp_path / "nope.md"), **kw))
    assert "extracted_from_sha256" not in entry and entry["origin_path"].endswith("nope.md")
    entry = add_fact.build_entry(add_fact.NewFact(**kw))
    assert "origin_path" not in entry and "extracted_from_sha256" not in entry
    entry = add_fact.build_entry(add_fact.NewFact(origin_path="   ", **kw))
    assert "origin_path" not in entry


# ------------------------------------------------------------------ backfill tool

def test_backfill_dry_run_apply_idempotent_and_byte_preserving(tmp_path):
    from ingest import backfill_extracted_hashes as bf
    data = tmp_path / "data"
    note = write(tmp_path / "vault" / "n.md", b"note body")
    items = [{"subject": "a", "statement": "One.", "trust_level": "low", "origin_path": note},
             {"subject": "a", "statement": "Two.", "trust_level": "low", "notes": "via notes"},
             {"subject": "a", "statement": "Three.", "trust_level": "low", "origin_path": str(tmp_path / "gone.md")},
             {"subject": "a", "statement": "Four.", "trust_level": "low"},
             {"subject": "a", "statement": "Five.", "trust_level": "low", "origin_path": note, "extracted_from_sha256": "keep"}]
    text = json.dumps(items, indent=2)
    write(data / "pilot_facts.json", text.encode())
    resolve = lambda notes: note if notes == "via notes" else None  # noqa: E731

    res, unresolved = bf.backfill(str(data), apply=False, resolve=resolve)
    assert res["pilot_facts.json"] == 2 and len(unresolved) == 1
    assert (data / "pilot_facts.json").read_text() == text  # dry run wrote nothing

    bf.backfill(str(data), apply=True, resolve=resolve)
    new = json.loads((data / "pilot_facts.json").read_text())
    assert [e.get("extracted_from_sha256") for e in new] == [sha(b"note body"), sha(b"note body"), None, None, "keep"]
    assert [{k: v for k, v in e.items() if k != "extracted_from_sha256"} for e in new] == \
           [{k: v for k, v in e.items() if k != "extracted_from_sha256"} for e in items]

    res, _ = bf.backfill(str(data), apply=True, resolve=resolve)
    assert res["pilot_facts.json"] == 0  # idempotent


def test_backfill_default_resolver_is_the_facts_step_rule():
    """The tool used to look the resolver up on a step module and crashed when the rule moved: the default
    must be the rule the facts step applies."""
    import paths
    from ingest import backfill_extracted_hashes as bf, fact_ingest_rules
    resolve = bf._resolver()
    assert resolve(None) is None
    notes = "Some Note.md, extracted 2026-01-01"
    assert resolve(notes) == fact_ingest_rules.resolve_origin_path(notes, paths.BODYBUILDING_VAULT) != None  # noqa: E711


# ------------------------------------------------------------------ CLI

def test_audit_sources_cli_exit_codes_and_json(tmp_path):
    from test_cli_wiring import Env
    env = Env(tmp_path)
    p = write(tmp_path / "src.md", b"v1")
    env.sql("UPDATE vault_files SET path = ? WHERE id = 1", p)
    env.sql("UPDATE facts SET extracted_from_sha256 = ? WHERE id = 1", sha(b"v1"))
    r = env.cli("audit-sources")
    assert (r.returncode, r.stdout) == (0, "No source file has changed since extraction.\n")
    write(p, b"v2")
    r = env.cli("audit-sources")
    assert r.returncode == 1 and "fact #1 changed" in r.stdout and p in r.stdout
    r = env.cli("audit-sources", "--json")
    out = json.loads(r.stdout)
    assert r.returncode == 1 and out["changed_sources"][0]["reason"] == "changed" and "no_baseline" in out


# ------------------------------------------------------------------ the work done, not just the result

def test_the_moved_file_search_is_skipped_when_a_known_vault_file_already_has_the_hash(tmp_path, monkeypatch):
    con = make_db()
    old = write(tmp_path / "a.md", b"one")
    add_fact(con, 1, old, sha(b"one"))
    os.rename(old, str(tmp_path / "c.md"))
    con.execute("INSERT INTO vault_files (path) VALUES (?)", (str(tmp_path / "c.md"),))
    monkeypatch.setattr(fixity_store, "_index_roots", lambda *a, **k: pytest.fail("walked the vault for nothing"))
    (row,) = fixity_store.audit_sources(con, search_roots=[str(tmp_path)])
    assert row["reason"] == "moved" and row["moved_to"] == str(tmp_path / "c.md")


def test_the_moved_file_search_stops_at_the_last_wanted_hash(tmp_path, monkeypatch):
    con = make_db()
    old = write(tmp_path / "gone.md", b"target")
    add_fact(con, 1, old, sha(b"target"))
    os.remove(old)
    for i in range(30):                       # "a00.md" .. "a29.md" sort before "z.md"; the target sits in "b.md"
        write(tmp_path / "vault" / f"a{i:02d}.md", f"other {i}".encode())
    write(tmp_path / "vault" / "b.md", b"target")
    for i in range(30):
        write(tmp_path / "vault" / f"c{i:02d}.md", f"after {i}".encode())
    hashed = []
    real = fixity_store.sha256_of
    monkeypatch.setattr(fixity_store, "sha256_of", lambda p: hashed.append(p) or real(p))
    (row,) = fixity_store.audit_sources(con, search_roots=[str(tmp_path / "vault")])
    assert (row["reason"], row["moved_to"]) == ("moved", str(tmp_path / "vault" / "b.md"))
    after = [p for p in hashed if os.path.basename(p).startswith("c")]
    assert not after, f"kept hashing after the target was found: {len(after)} files"
    assert len(hashed) <= 32            # 30 files before the target + the target + the failed lookup of the missing file


def test_the_moved_file_search_does_not_reread_files_it_already_hashed(tmp_path, monkeypatch):
    con = make_db()
    gone = write(tmp_path / "gone.md", b"target")
    add_fact(con, 1, gone, sha(b"target"))
    os.remove(gone)
    known = write(tmp_path / "known.md", b"not the target")
    con.execute("INSERT INTO vault_files (path) VALUES (?)", (known,))
    write(tmp_path / "vault" / "other.md", b"also not")
    hashed = []
    real = fixity_store.sha256_of
    monkeypatch.setattr(fixity_store, "sha256_of", lambda p: hashed.append(p) or real(p))
    (row,) = fixity_store.audit_sources(con, search_roots=[str(tmp_path)])
    assert row["reason"] == "missing"
    assert hashed.count(known) == 1, "hashed once for the known-path lookup, never again by the walk"


def test_a_vault_file_cited_by_many_facts_is_hashed_and_read_for_attributes_once(tmp_path, monkeypatch):
    import acquisition_store
    from ingest import shared
    note = write(tmp_path / "n.md", b"note body")
    con = sqlite3.connect(":memory:")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    cur = con.cursor()
    fingerprints, attr_reads = [], []
    real_fp, real_resolve = fixity_store.fingerprint, acquisition_store.resolve
    monkeypatch.setattr(fixity_store, "fingerprint", lambda p: fingerprints.append(p) or real_fp(p))
    monkeypatch.setattr(acquisition_store, "resolve", lambda d, p, **k: attr_reads.append(p) or real_resolve(d, p, **k))
    ids = {shared.get_or_create_vault_file(cur, note) for _ in range(5)}
    assert len(ids) == 1 and fingerprints == [note] and attr_reads == [note]
    row = con.execute("SELECT content_sha256, file_state FROM vault_files").fetchone()
    assert tuple(row) == (sha(b"note body"), "present")


def test_a_vault_file_row_created_without_a_fingerprint_is_still_filled_in(tmp_path):
    from ingest import shared
    note = write(tmp_path / "n.md", b"body")
    con = sqlite3.connect(":memory:")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    cur = con.cursor()
    cur.execute("INSERT INTO vault_files (path) VALUES (?)", (note,))
    shared.get_or_create_vault_file(cur, note)
    assert tuple(con.execute("SELECT content_sha256, file_state FROM vault_files").fetchone()) == (sha(b"body"), "present")
