"""The source lane: the application service against a fake repository, the SQLite adapter against the real schema,
and the composed readers against an in-memory world. No file system, no private checkout."""
import dataclasses
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import source_ingest_service as svc
from ingest import source_readers
from locations import Locations
from source_record import IdentifierConflict, SourceRecord
from source_repository_store import SqliteSourceRepository
from source_status import Relation, SourceState, SourceStatusError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rec(citekey, **kw):
    return SourceRecord(citekey=citekey, name=kw.pop("name", citekey), source_type="primary", **kw)


class FakeRepo:
    def __init__(self, known=()):
        self.sources, self.relations, self.files = [], [], {}
        self._known = set(known)
        self.conflicts = {}

    def citekeys(self):
        return set(self._known) | {r.citekey for r in self.sources}

    def add_source(self, record):
        self.sources.append(record)
        return self.conflicts.get(record.citekey, [])

    def add_relations(self, relations):
        self.relations.extend(relations)

    def status_counts(self):
        return {}

    def recorded_vault_file(self, path):
        return self.files.get(path)

    def record_vault_file(self, path, fingerprint, acquisition):
        self.files[path] = len(self.files) + 1
        return self.files[path]


# ---------------------------------------------------------------- application service

def test_add_sources_stores_in_order_and_reports_identifier_problems_and_conflicts():
    repo, lines = FakeRepo(), []
    repo.conflicts["b"] = [IdentifierConflict("doi", "10.1/x", "a")]
    n = svc.add_sources(repo, [rec("a"), rec("b", identifier_problems=("doi 'zz': bad",))], report=lines.append)
    assert n == 2 and [r.citekey for r in repo.sources] == ["a", "b"]
    assert lines == ["  WARNING -- doi 'zz': bad; identifier not recorded",
                     "  WARNING -- duplicate identifier doi:10.1/x shared by sources 'a' and 'b' (not merged)"]


def test_skip_known_skips_stored_citekeys_and_repeats_within_the_batch():
    repo = FakeRepo(known={"manual"})
    n = svc.add_sources(repo, [rec("manual"), rec("n1"), rec("n1"), rec("n2")], skip_known=True)
    assert n == 2 and [r.citekey for r in repo.sources] == ["n1", "n2"]


def test_without_skip_known_every_record_is_offered_to_the_repository():
    repo = FakeRepo(known={"a"})
    assert svc.add_sources(repo, [rec("a"), rec("a")]) == 2


@pytest.mark.parametrize("rels, msg", [
    ([("a", "replaces", "ghost")], "no source has the citekey 'ghost'"),
    ([("a", "replaces", "a")], "cannot replaces itself|cannot replace itself"),
    ([("a", "replaces", "b"), ("b", "replaces", "a")], "replaces cycle"),
    ([("a", "obsoletes", "b")], "must be one of"),
])
def test_link_relations_validates_before_storing_anything(rels, msg):
    repo = FakeRepo(known={"a", "b"})
    with pytest.raises(SourceStatusError, match=msg):
        svc.link_relations(repo, rels)
    assert repo.relations == []


def test_link_relations_collapses_duplicates_and_stores_the_rest():
    repo = FakeRepo(known={"a", "b"})
    out = svc.link_relations(repo, [("a", "replaces", "b"), ("a", "replaces", "b"), ("a", "is-version-of", "b")])
    assert out == repo.relations == [Relation("a", "replaces", "b"), Relation("a", "is-version-of", "b")]


def test_vault_file_id_reads_the_file_once_and_none_for_no_path():
    repo, reads = FakeRepo(), []
    inputs = SimpleNamespace(fingerprint=lambda p: reads.append(("fp", p)) or {}, acquisition=lambda r, p: reads.append(("acq", p)) or r)
    assert svc.vault_file_id(repo, inputs, None) is None and svc.vault_file_id(repo, inputs, "") is None
    ids = {svc.vault_file_id(repo, inputs, "/v/n.md") for _ in range(3)}
    assert len(ids) == 1 and reads == [("fp", "/v/n.md"), ("acq", "/v/n.md")]


def test_no_caller_discards_the_conflicts_add_source_returns():
    """add_source's result is the only record of a refused identifier; the service must be its only caller."""
    import ast
    import glob
    offenders = []
    for path in glob.glob(os.path.join(REPO, "**", "*.py"), recursive=True):
        if "/.venv/" in path or "/tests/" in path or os.path.basename(path) in ("source_repository_store.py",):
            continue
        for node in ast.walk(ast.parse(open(path).read())):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and getattr(node.value.func, "attr", "") == "add_source":
                offenders.append(path)
    assert offenders == []


# ---------------------------------------------------------------- SQLite adapter

@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(open(os.path.join(REPO, "schema.sql")).read())
    return c


FP = {"content_sha256": "a" * 64, "size_bytes": 3, "file_mtime": "2026-01-01T00:00:00Z", "mime_type": "text/markdown", "file_state": "present"}


def test_adapter_stores_a_source_with_publisher_authors_state_and_fingerprint(con):
    repo = SqliteSourceRepository(con)
    state = SourceState.from_raw({"status": "retracted", "status_date": "2020-05", "edition": "2nd"}, "w")
    repo.add_source(rec("a2020", publisher="JCEM", state=state, fingerprint=FP, retrieved_date="2026-09-11",
                        authors=((0, "Smith, J"), (2, "Lee, K")),
                        acquisition={"acquired_at": "2024", "acquired_via": "download", "where_from": None, "acquired_note": None}))
    repo.add_source(rec("b2021", publisher="JCEM", authors=((0, "Smith, J"),)))
    r = con.execute("SELECT s.*, p.name AS pub FROM sources s JOIN publishers p ON p.id = s.publisher_id WHERE citekey = 'a2020'").fetchone()
    assert (r["pub"], r["status"], r["status_date"], r["edition"], r["content_sha256"], r["file_state"], r["acquired_via"], r["retrieved_date"]) == \
        ("JCEM", "retracted", "2020-05", "2nd", "a" * 64, "present", "download", "2026-09-11")
    assert con.execute("SELECT COUNT(*) FROM publishers").fetchone()[0] == 1 and con.execute("SELECT COUNT(*) FROM authors").fetchone()[0] == 2
    assert [tuple(x) for x in con.execute("SELECT a.name, sa.author_order FROM source_authors sa JOIN authors a ON a.id = sa.author_id "
                                          "JOIN sources s ON s.id = sa.source_id WHERE s.citekey = 'a2020' ORDER BY 2")] == [("Smith, J", 0), ("Lee, K", 2)]
    m = con.execute("SELECT content_sha256, file_state, status FROM sources WHERE citekey = 'b2021'").fetchone()
    assert tuple(m) == (None, None, "active")


def test_adapter_keeps_a_second_claim_on_an_identifier_as_a_conflict(con):
    repo = SqliteSourceRepository(con)
    assert repo.add_source(rec("first", identifiers=(("doi", "10.1/x"),))) == []
    assert repo.add_source(rec("second", identifiers=(("doi", "10.1/x"), ("pmid", "7")))) == [IdentifierConflict("doi", "10.1/x", "first")]
    assert [tuple(r) for r in con.execute("SELECT s.citekey, i.scheme, i.value FROM source_identifiers i JOIN sources s ON s.id = i.source_id ORDER BY 1, 2")] == \
        [("first", "doi", "10.1/x"), ("second", "pmid", "7")]
    assert con.execute("SELECT COUNT(*) FROM source_identifier_conflicts").fetchone()[0] == 1


def test_adapter_relations_citekeys_and_status_counts(con):
    repo = SqliteSourceRepository(con)
    repo.add_source(rec("old", state=SourceState.from_raw({"status": "superseded"}, "w")))
    repo.add_source(rec("new"))
    repo.add_relations([Relation("new", "replaces", "old")])
    assert repo.citekeys() == {"old", "new"} and repo.status_counts() == {"superseded": 1, "active": 1}
    assert [tuple(r) for r in con.execute("SELECT source_id, relation, related_source_id FROM source_relations")] == [(2, "replaces", 1)]


def test_adapter_vault_file_is_recorded_once_and_a_bare_row_is_filled_in(con):
    repo = SqliteSourceRepository(con)
    con.execute("INSERT INTO vault_files (path) VALUES ('/v/n.md')")
    assert repo.recorded_vault_file("/v/n.md") is None and repo.recorded_vault_file("/v/other.md") is None
    acq = {"acquired_at": None, "acquired_via": "manual", "where_from": None, "acquired_note": "from attributes"}
    fid = repo.record_vault_file("/v/n.md", FP, acq)
    assert fid == 1 and repo.recorded_vault_file("/v/n.md") == 1 and repo.record_vault_file("/v/m.md", FP, acq) == 2
    assert tuple(con.execute("SELECT file_state, acquired_via, acquired_note FROM vault_files WHERE id = 1").fetchone()) == \
        ("present", "manual", "from attributes")


# ---------------------------------------------------------------- composed readers

class World:
    """An in-memory `Inputs`: files by path, and a count of how often each was read."""
    def __init__(self, files):
        self.files, self.reads = files, []

    def read_json(self, path):
        self.reads.append(path)
        return json.loads(self.files[path])

    def read_text(self, path, encoding="utf-8"):
        self.reads.append(path)
        return self.files[path]

    def list_files(self, directory, pattern):
        return sorted(p for p in self.files if os.path.dirname(p) == directory and p.endswith(".md"))

    def fingerprint(self, path):
        return dict(FP)

    def acquisition(self, row, path):
        return dict(row)


LOCS = Locations(knowledge_db_dir="/db", private_data_dir="/data", notes_vault="/vault", health_dir="/health")
NOTE = "---\ntitle: T\nyear: 2020\ndoi: 10.1210/jc.2007-1692\nauthors:\n  - \"Lee, K\"\nreplaces:\n  - \"old\"\n---\nbody\n"


def world(manual):
    return World({"/data/manual_sources.json": json.dumps(manual), "/vault/sources/n2020.md": NOTE, "/vault/sources/README.md": "skip"})


def test_composed_readers_yield_manual_sources_then_notes_as_validated_records():
    w = world([{"citekey": "m1", "name": "M", "source_type": "primary", "origin_path": "{VAULT}/m.md", "author": "A;;B",
                "doi": "10.5555/ABC123", "isbn": "bogus", "replaces": ["n2020"], "status": "retracted", "status_date": "2024"}])
    records = source_readers.default_readers(SimpleNamespace(inputs=w, locations=LOCS)).records()
    assert [r.citekey for r in records] == ["m1", "n2020"]
    m, n = records
    assert m.origin_path == "/vault/m.md" and m.authors == ((0, "A"), (2, "B")) and m.fingerprint is None
    assert m.state.status.value == "retracted" and m.identifiers == (("doi", "10.5555/abc123"),) and len(m.identifier_problems) == 1
    assert m.relations == (Relation("m1", "replaces", "n2020"),)
    assert n.retrieved_date == source_readers.NOTE_RETRIEVED_DATE and n.fingerprint == FP
    assert n.identifiers == (("doi", "10.1210/jc.2007-1692"),) and n.relations == (Relation("n2020", "replaces", "old"),)


def test_origin_paths_are_stored_as_the_vault_and_health_are_written_in_local_paths():
    locs = dataclasses.replace(LOCS, notes_vault_written="~/vault", health_dir_written="~/health")
    w = world([{"citekey": "m1", "name": "M", "source_type": "primary", "origin_path": "{VAULT}/m.md"},
               {"citekey": "h1", "name": "H", "source_type": "primary", "origin_path": "{HEALTH}/h.md"}])
    records = {r.citekey: r for r in source_readers.default_readers(SimpleNamespace(inputs=w, locations=locs)).records()}
    assert records["m1"].origin_path == "~/vault/m.md" and records["h1"].origin_path == "~/health/h.md"
    assert records["n2020"].origin_path.startswith("~/vault/")      # the note is still read from the expanded vault


def test_relations_are_read_afresh_each_time_not_from_a_snapshot():
    w = world([{"citekey": "m1", "name": "M", "source_type": "primary", "replaces": ["n2020"]}])
    reader = source_readers.default_readers(SimpleNamespace(inputs=w, locations=LOCS))
    assert reader.records() and w.reads.count("/data/manual_sources.json") == 1 and w.reads.count("/vault/sources/n2020.md") == 1
    assert reader.relations() == [Relation("m1", "replaces", "n2020"), Relation("n2020", "replaces", "old")]
    assert w.reads.count("/data/manual_sources.json") == 2 and w.reads.count("/vault/sources/n2020.md") == 2
    w.files["/data/manual_sources.json"] = "[]"          # a change after step 01 is seen by the relations step
    assert reader.relations() == [Relation("n2020", "replaces", "old")]


def test_manual_relations_do_not_validate_the_rest_of_the_source():
    w = world([{"citekey": "m1", "name": "M", "source_type": "primary", "status": "banned", "replaces": ["n2020"]}])
    manual = source_readers.ManualSources(w, LOCS)
    assert manual.relations() == [Relation("m1", "replaces", "n2020")]
    with pytest.raises(SourceStatusError, match="m1"):
        manual.records()


def test_no_vault_means_no_notes_and_a_missing_placeholder_value_fails():
    no_vault = Locations(knowledge_db_dir="/db", private_data_dir="/data")
    assert source_readers.VaultNotes(world([]), no_vault).records() == []
    w = world([{"citekey": "m1", "name": "M", "source_type": "primary", "origin_path": "{VAULT}/m.md"}])
    with pytest.raises(ValueError, match="NOTES_VAULT"):
        source_readers.ManualSources(w, no_vault).relations()
