"""#46: custodial history (acquired_at / acquired_via / where_from) for sources and vault files.

Data first; the macOS file attributes only fill gaps and are marked; every failure to read them is a silent
no-op. Most tests simulate the attributes through the injectable runner so they pass on any platform; a few
write real attributes onto temp files with the `xattr` command and are skipped where it is missing.
"""
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import tempfile

import pytest

import acquisition_store as aq
from test_add_fact import REPO
from test_fact_ingest import F, ingest  # noqa: F401  (fixture)

DARWIN = aq.available()
needs_xattr = pytest.mark.skipif(not DARWIN, reason="needs macOS and the xattr command")


@pytest.fixture(autouse=True)
def _restore_modules():
    before = dict(sys.modules)
    yield
    for name in set(sys.modules) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


def plist_hex(urls):
    raw = plistlib.dumps(urls, fmt=plistlib.FMT_BINARY).hex().upper()
    return "\n".join(" ".join(raw[i:i + 2] for i in range(j, min(j + 32, len(raw)), 2)) for j in range(0, len(raw), 32)) + "\n"


def fake_run(attrs, calls=None):
    """A runner that behaves like `xattr` for a file whose attributes are `attrs` {name: output text}."""
    def run(args):
        if calls is not None:
            calls.append(list(args))
        if args[:2] in (["xattr", "-p"], ["xattr", "-px"]):
            return attrs.get(args[2])
        if len(args) == 2:
            return "\n".join(attrs) + ("\n" if attrs else "")
        return None
    return run


QUARANTINE_5F3E1C2A = "0083;5f3e1c2a;Safari;ABCD-1234"       # 0x5f3e1c2a seconds since the epoch
WHEN = "2020-08-20T06:46:02+00:00"


# ------------------------------------------------------------------ data normalization

def test_normalize_data_defaults_blank_and_valid_values():
    assert aq.normalize_data({}, "w") == {"acquired_at": None, "acquired_via": None, "where_from": None, "acquired_note": None}
    out = aq.normalize_data({"acquired_at": " 2024-03-05 ", "acquired_via": "manual", "where_from": " https://x.example ", "acquired_note": "  "}, "w")
    assert out == {"acquired_at": "2024-03-05", "acquired_via": "manual", "where_from": "https://x.example", "acquired_note": None}
    for when in ("2024", "2024-03", "2024-03-05", "2024-03-05T10:00:00+00:00", "2024-03-05T10:00:00"):
        assert aq.normalize_data({"acquired_at": when}, "w")["acquired_at"] == when
    for via in aq.VIA_VALUES:
        assert aq.normalize_data({"acquired_via": via}, "w")["acquired_via"] == via


@pytest.mark.parametrize("bad", [{"acquired_at": "last tuesday"}, {"acquired_at": "2024-13"}, {"acquired_via": "scanned"}, {"acquired_via": "Download"},
                                 {"where_from": 5}, {"acquired_at": 2024}])
def test_normalize_data_rejects_bad_values_naming_the_source(bad):
    with pytest.raises(aq.AcquisitionError, match="source 'x'"):
        aq.normalize_data(bad, "source 'x'")


# ------------------------------------------------------------------ parsing the attributes

def test_parse_where_froms_takes_the_first_url_from_hex_in_any_layout():
    urls = ["https://example.org/paper.pdf", "https://example.org/landing"]
    assert aq._parse_where_froms(plist_hex(urls)) == urls[0]
    assert aq._parse_where_froms(plist_hex(urls).replace(" ", "").replace("\n", "")) == urls[0]
    assert aq._parse_where_froms(plist_hex(["", "  ", "https://b.example"])) == "https://b.example"


@pytest.mark.parametrize("junk", ["", "zz", "62 70 6c", "not hex at all", "00", plist_hex([]), plist_hex("a string"), plist_hex({"k": "v"}), plist_hex([1, 2])])
def test_parse_where_froms_garbage_is_none(junk):
    assert aq._parse_where_froms(junk) is None


def test_parse_quarantine():
    assert aq._parse_quarantine(QUARANTINE_5F3E1C2A) == WHEN
    assert aq._parse_quarantine(QUARANTINE_5F3E1C2A + "\n") == WHEN
    for junk in ("", "nope", "0083;;Safari;", "0083;zz;Safari;", "0083;0;Safari;", "0083;ffffffffffff;Safari;"):
        assert aq._parse_quarantine(junk) is None, junk


# ------------------------------------------------------------------ reading (simulated)

def test_read_attributes_both_one_and_none():
    where = {aq.WHERE_FROMS: plist_hex(["https://example.org/a.pdf"])}
    quar = {aq.QUARANTINE: QUARANTINE_5F3E1C2A}
    assert aq.read_attributes("/f", run=fake_run({**where, **quar}), platform_ok=True) == {"where_from": "https://example.org/a.pdf", "acquired_at": WHEN}
    assert aq.read_attributes("/f", run=fake_run(where), platform_ok=True) == {"where_from": "https://example.org/a.pdf"}
    assert aq.read_attributes("/f", run=fake_run(quar), platform_ok=True) == {"acquired_at": WHEN}
    assert aq.read_attributes("/f", run=fake_run({"com.apple.FinderInfo": "00"}), platform_ok=True) == {}
    assert aq.read_attributes("/f", run=fake_run({}), platform_ok=True) == {}


def test_read_attributes_is_a_silent_no_op_when_anything_is_off():
    calls = []
    assert aq.read_attributes("/f", run=fake_run({aq.QUARANTINE: QUARANTINE_5F3E1C2A}, calls), platform_ok=False) == {} and calls == []   # not macOS
    assert aq.read_attributes("", run=fake_run({}, calls), platform_ok=True) == {} and aq.read_attributes(None, run=fake_run({}, calls), platform_ok=True) == {}
    assert calls == []
    assert aq.read_attributes("/f", run=lambda args: None, platform_ok=True) == {}                    # xattr failed / file unreadable
    assert aq.read_attributes("/f", run=fake_run({aq.WHERE_FROMS: "garbage"}), platform_ok=True) == {}   # unparseable plist
    assert aq.read_attributes("/f", run=fake_run({aq.QUARANTINE: "garbage"}), platform_ok=True) == {}


def test_default_runner_decodes_raw_bytes_instead_of_failing(monkeypatch):
    captured = {}
    def fake(args, **kw):
        captured.update(kw)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    assert aq._default_run(["xattr", "/f"]) == "ok" and captured["errors"] == "replace" and captured["timeout"] == 5


def test_default_runner_swallows_a_missing_command_and_a_timeout(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("xattr")
    monkeypatch.setattr(subprocess, "run", boom)
    assert aq._default_run(["xattr", "/f"]) is None
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("xattr", 5)
    monkeypatch.setattr(subprocess, "run", slow)
    assert aq._default_run(["xattr", "/f"]) is None


def test_it_only_ever_runs_read_only_xattr_argv_shapes(tmp_path):
    calls = []
    aq.read_attributes(str(tmp_path / "f"), run=fake_run({aq.WHERE_FROMS: plist_hex(["https://a.example"]), aq.QUARANTINE: QUARANTINE_5F3E1C2A}, calls), platform_ok=True)
    f = str(tmp_path / "f")
    assert calls == [["xattr", f], ["xattr", "-px", aq.WHERE_FROMS, f], ["xattr", "-p", aq.QUARANTINE, f]]
    source = open(os.path.join(REPO, "acquisition_store.py")).read()
    assert not re.search(r'"-(w|wx|d|c|r|s)"', source)                            # no write / delete / clear flag anywhere
    assert source.count("subprocess.run") == 1 and "shell=True" not in source


def test_tilde_paths_are_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    aq.read_attributes("~/v/n.pdf", run=fake_run({}, calls), platform_ok=True)
    assert calls[0] == ["xattr", str(tmp_path / "v" / "n.pdf")]


# ------------------------------------------------------------------ precedence

NONE = {k: None for k in aq.COLUMNS}
ATTRS = fake_run({aq.WHERE_FROMS: plist_hex(["https://example.org/a.pdf"]), aq.QUARANTINE: QUARANTINE_5F3E1C2A})


def test_attributes_fill_every_gap_and_are_marked():
    out = aq.resolve(dict(NONE), "/f", run=ATTRS, platform_ok=True)
    assert out == {"acquired_at": WHEN, "acquired_via": "download", "where_from": "https://example.org/a.pdf",
                   "acquired_note": "from macOS file attributes: where_from, acquired_at"}


def test_data_is_never_overwritten_field_by_field():
    out = aq.resolve({**NONE, "acquired_at": "2019-01-01", "where_from": "https://mine.example"}, "/f", run=ATTRS, platform_ok=True)
    assert (out["acquired_at"], out["where_from"]) == ("2019-01-01", "https://mine.example")
    assert out["acquired_note"] is None and out["acquired_via"] is None             # nothing came from the attributes
    out = aq.resolve({**NONE, "acquired_at": "2019-01-01"}, "/f", run=ATTRS, platform_ok=True)
    assert out["acquired_at"] == "2019-01-01" and out["where_from"] == "https://example.org/a.pdf"
    assert out["acquired_note"] == "from macOS file attributes: where_from" and out["acquired_via"] == "download"


def test_data_via_wins_and_a_data_note_is_kept_with_the_marker_appended():
    out = aq.resolve({**NONE, "acquired_via": "manual", "acquired_note": "typed in from the paper copy"}, "/f", run=ATTRS, platform_ok=True)
    assert out["acquired_via"] == "manual"
    assert out["acquired_note"] == "typed in from the paper copy; from macOS file attributes: where_from, acquired_at"


def test_no_data_and_no_attributes_stays_null_and_nothing_is_invented():
    assert aq.resolve(dict(NONE), "/f", run=fake_run({}), platform_ok=True) == NONE
    assert aq.resolve(dict(NONE), "/f", run=ATTRS, platform_ok=False) == NONE
    only = {**NONE, "acquired_via": "export"}
    assert aq.resolve(dict(only), "/f", run=ATTRS, platform_ok=False) == only


# ------------------------------------------------------------------ real attributes (macOS only)

def write_attr(path, name, text=None, hex_=None):
    args = ["xattr", "-wx", name, hex_.replace("\n", " "), path] if hex_ else ["xattr", "-w", name, text, path]
    subprocess.run(args, check=True, capture_output=True)


@needs_xattr
def test_real_attributes_on_a_temp_file(tmp_path):
    p = str(tmp_path / "dl.pdf")
    open(p, "w").write("x")
    assert aq.read_attributes(p) == {}                                             # a file with no attributes
    write_attr(p, aq.WHERE_FROMS, hex_=plist_hex(["https://example.org/real.pdf", "https://example.org/ref"]))
    write_attr(p, aq.QUARANTINE, text=QUARANTINE_5F3E1C2A)
    assert aq.read_attributes(p) == {"where_from": "https://example.org/real.pdf", "acquired_at": WHEN}
    assert aq.read_attributes(str(tmp_path / "missing.pdf")) == {}
    assert aq.read_attributes(str(tmp_path)) == {}                                 # a directory


@needs_xattr
def test_reading_never_changes_the_file_or_its_attributes(tmp_path):
    p = str(tmp_path / "dl.pdf")
    open(p, "w").write("x")
    write_attr(p, aq.QUARANTINE, text=QUARANTINE_5F3E1C2A)
    before = (os.stat(p).st_mtime_ns, subprocess.run(["xattr", "-l", p], capture_output=True, text=True).stdout)
    aq.read_attributes(p)
    aq.resolve(dict(NONE), p)
    assert (os.stat(p).st_mtime_ns, subprocess.run(["xattr", "-l", p], capture_output=True, text=True).stdout) == before


# ------------------------------------------------------------------ ingest

def note_text(extra=""):
    return f"---\ntitle: T\nyear: 2020\nsource-type: peer-reviewed-study\n{extra}---\nbody\n"


def run02(ingest, tmp_path, monkeypatch, notes, attrs=None):
    d = tmp_path / "vault" / "sources"
    d.mkdir(parents=True)
    for name, extra in notes.items():
        (d / f"{name}.md").write_text(note_text(extra))
    import acquisition_store
    monkeypatch.setattr(acquisition_store, "read_attributes", lambda path, run=None, platform_ok=None: dict(attrs.get(os.path.basename(path), {})) if attrs else {})
    spec = __import__("importlib.util").util.spec_from_file_location("m02", os.path.join(REPO, "02_ingest_literature_sources.py"))
    mod = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SRC_DIR = str(d)
    con = ingest.db()
    mod.run(con)
    return con


def test_02_without_data_or_attributes_leaves_the_columns_null(ingest, tmp_path, monkeypatch):
    con = run02(ingest, tmp_path, monkeypatch, {"a2020": ""})
    r = con.execute("SELECT * FROM sources").fetchone()
    assert (r["acquired_at"], r["acquired_via"], r["where_from"], r["acquired_note"]) == (None, None, None, None)


def test_02_frontmatter_wins_and_attributes_fill_the_rest(ingest, tmp_path, monkeypatch):
    attrs = {"a2020.md": {"where_from": "https://example.org/from-attrs", "acquired_at": WHEN},
             "b2021.md": {"where_from": "https://example.org/b", "acquired_at": WHEN}}
    con = run02(ingest, tmp_path, monkeypatch,
                {"a2020": "acquired-at: 2019-06-01\nacquired-via: manual\nwhere-from: https://example.org/mine\n", "b2021": ""}, attrs)
    rows = {r["citekey"]: r for r in con.execute("SELECT * FROM sources")}
    a, b = rows["a2020"], rows["b2021"]
    assert (a["acquired_at"], a["acquired_via"], a["where_from"], a["acquired_note"]) == ("2019-06-01", "manual", "https://example.org/mine", None)
    assert (b["acquired_at"], b["acquired_via"], b["where_from"]) == (WHEN, "download", "https://example.org/b")
    assert b["acquired_note"] == "from macOS file attributes: where_from, acquired_at"


def test_02_a_bad_value_in_frontmatter_fails_the_build_naming_the_note(ingest, tmp_path, monkeypatch):
    with pytest.raises(Exception, match="bad2020") as e:
        run02(ingest, tmp_path, monkeypatch, {"bad2020": "acquired-via: scanned\n"})
    assert type(e.value).__name__ == "AcquisitionError"


def test_01_manual_sources_take_data_first(ingest, monkeypatch):
    import acquisition_store
    monkeypatch.setattr(acquisition_store, "read_attributes", lambda path, run=None, platform_ok=None: {"where_from": "https://attrs.example", "acquired_at": WHEN} if path else {})
    with open(os.path.join(ingest.env.data_dir, "manual_sources.json"), "w") as f:
        json.dump([{"citekey": "m1", "name": "M", "source_type": "primary", "origin_path": "/x/m1.pdf", "where_from": "https://data.example", "acquired_via": "export"},
                   {"citekey": "m2", "name": "M2", "source_type": "primary"}], f)
    spec = __import__("importlib.util").util.spec_from_file_location("m01", os.path.join(REPO, "01_seed_sources.py"))
    mod = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    con = ingest.db()
    mod.run(con)
    rows = {r["citekey"]: r for r in con.execute("SELECT * FROM sources")}
    assert (rows["m1"]["where_from"], rows["m1"]["acquired_via"], rows["m1"]["acquired_at"]) == ("https://data.example", "export", WHEN)
    assert rows["m1"]["acquired_note"] == "from macOS file attributes: acquired_at"
    assert (rows["m2"]["where_from"], rows["m2"]["acquired_at"], rows["m2"]["acquired_note"]) == (None, None, None)       # no origin_path: nothing to read


def test_vault_files_get_attributes_only_and_counts_are_unchanged(ingest, tmp_path, monkeypatch):
    import acquisition_store
    seen = []
    monkeypatch.setattr(acquisition_store, "read_attributes", lambda path, run=None, platform_ok=None: seen.append(path) or {"where_from": "https://example.org/v", "acquired_at": WHEN})
    note = tmp_path / "n.md"
    note.write_text("x")
    con = ingest.run04([F(origin_path=str(note)), F(statement="Second.", origin_path=str(note)), F(statement="None.")])
    assert con.execute("SELECT COUNT(*) FROM vault_files").fetchone()[0] == 1 and con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 3
    r = con.execute("SELECT * FROM vault_files").fetchone()
    assert (r["where_from"], r["acquired_at"], r["acquired_via"]) == ("https://example.org/v", WHEN, "download")
    assert r["acquired_note"].startswith("from macOS file attributes")
    assert r["path"] == str(note) and r["content_sha256"] is not None                  # fixity columns untouched by this change


def test_schema_check_on_acquired_via():
    con = sqlite3.connect(":memory:")
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    con.execute("INSERT INTO sources (id, name, source_type) VALUES (1, 'A', 'primary')")
    con.execute("INSERT INTO vault_files (id, path) VALUES (1, 'p')")
    for via in (None, *aq.VIA_VALUES):
        con.execute("UPDATE sources SET acquired_via = ? WHERE id = 1", (via,))
        con.execute("UPDATE vault_files SET acquired_via = ? WHERE id = 1", (via,))
    for table in ("sources", "vault_files"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(f"UPDATE {table} SET acquired_via = 'scanned'")


# ------------------------------------------------------------------ normal-only DB

def test_the_normal_db_has_none_of_the_custodial_columns_and_the_leak_test_has_a_marker():
    import leak_test
    import normal_db
    import privacy
    with tempfile.TemporaryDirectory() as d:
        full = leak_test.build_fixture(d)
        path, _ = normal_db.build_normal_atomic(full, d, privacy.Rules())
        con = sqlite3.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(sources)")}
        assert not ({"acquired_at", "acquired_via", "where_from", "acquired_note"} & cols)
        assert leak_test.MARKERS["where_from URL"].encode() not in open(path, "rb").read()
        assert not leak_test.scan_against_full(path, full)
        con.execute("UPDATE sources SET edition = ? WHERE id = 1", (leak_test.MARKERS["where_from URL"],))
        con.commit()
        con.close()
        assert leak_test.scan_against_full(path, full)
