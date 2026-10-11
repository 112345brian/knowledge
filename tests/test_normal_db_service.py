"""The normal-db use case against in-memory fakes of its ports: no file, no sqlite. It proves the policy
(`normal_rules`, `leak_rules`) and the lifecycle (build, leak check, replace, remove the stale db on failure) are
decided in the domain and the service, not in an adapter."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import leak_rules  # noqa: E402
import normal_db_service as svc  # noqa: E402
import privacy  # noqa: E402
from normal_rules import FullDbRows, Integrity, NormalDbError, TABLES  # noqa: E402

DIR = "/out"
FINAL = os.path.join(DIR, "knowledge-normal.db")


def fact(fid, subject, statement, visibility="normal", key=None):
    # FACT_COLS order: id, subject_id, statement, is_original_claim, trust_level, trust_rationale, status,
    # superseded_by_fact_id, date_added, last_reviewed_at, recheck_by, recheck_rationale, visibility, source_key,
    # freshness, kind, valid_from, valid_to, applies_to
    return (fid, subject, statement, 0, "medium", None, "active", None, "2026-01-01", None, None, None, visibility,
            key or f"k{fid}", "unreviewed", "observation", None, None, None)


def rows(**over):
    base = dict(
        # (id, name, parent_id, private, domain, description, parent_relation, deprecated, replaced_by)
        subjects=[(1, "open", None, 0, "health", None, "broader", 0, None), (2, "secret", None, 1, "life", None, "broader", 0, None)],
        subject_aliases=[], citations=[],
        facts=[fact(1, 1, "Creatine is well studied."), fact(2, 2, "Under a private subject"), fact(3, 1, "Mentions wibblesnort")],
        revisions=[], sources=[], publishers=[], authors=[], source_authors=[], identifiers=[],
        entities=[], entity_aliases=[], fact_entities=[], build_info=None)
    base.update(over)
    return FullDbRows(**base)


class FakeFiles:
    def __init__(self, existing=()):
        self.files = set(existing)
        self.calls = []

    def exists(self, path): return path in self.files
    def unique_token(self): return "tok"
    def read_bytes(self, path):
        self.calls.append(("read_bytes", path))
        return self.raw if hasattr(self, "raw") else b""

    def create_exclusive(self, path): self.files.add(path)
    def remove_if_exists(self, path): self.files.discard(path)

    def replace(self, source, target):
        self.calls.append(("replace", source, target))
        self.files.discard(source)
        self.files.add(target)


class FakeSource:
    def __init__(self, data): self.data = data
    def read_rows(self, full_path): return self.data


class FakeSink:
    def __init__(self, files, integrity=None, fail=None):
        self.files, self.content, self.fail = files, None, fail
        self.integrity = integrity or Integrity([], {t: 0 for t in TABLES}, set(TABLES), 0)

    def write(self, path, content):
        if self.fail:
            raise self.fail
        self.files.files.add(path)
        self.content = {part.table: part for part in content}

    def inspect(self, path): return self.integrity


class FakeScan:
    def __init__(self, cells=(), material=None):
        self._cells, self._material = list(cells), material
        self.cell_paths = []

    def cells(self, path):
        self.cell_paths.append(path)
        return iter(self._cells)

    def private_material(self, full_path): return self._material


def material(**over):
    base = dict(legit=set(), private_facts=[], private_citations=[], private_revisions=[], claims=[], vault_paths=[],
                origin_paths=[], build_commits=[], build_input_keys=[], status_notes=[], where_from=[], file_hashes=[],
                private_subject_names=[], private_entities=[], private_entity_aliases=[],
                private_subject_descriptions=[], private_subject_aliases=[])
    base.update(over)
    return leak_rules.PrivateMaterial(**base)


def deps(data=None, files=None, scan=None, integrity=None, fail=None):
    files = files or FakeFiles()
    return svc.NormalDbDeps(files=files, source=FakeSource(data or rows()), sink=FakeSink(files, integrity, fail),
                            leak_scan=scan or FakeScan(material=material()))


def test_content_holds_only_rows_the_policy_lets_in():
    d = deps()
    counts = svc.build_normal_db(d, "/full.db", "/out/x.db", privacy.Rules(keywords=("wibblesnort",)))
    facts = d.sink.content["facts"].rows
    assert [r[0] for r in facts] == [1]                     # fact 2 is under a private subject, fact 3 trips a keyword
    assert [r[0] for r in d.sink.content["subjects"].rows] == [1]
    assert counts == d.sink.integrity.counts


def test_content_order_and_columns_are_the_policys_not_the_adapters():
    d = deps()
    svc.build_normal_db(d, "/full.db", "/out/x.db", privacy.Rules())
    assert list(d.sink.content) == ["facts", "subjects", "subject_aliases", "fact_revisions", "publishers", "sources",
                                    "authors", "source_authors", "fact_sources", "source_identifiers", "entities",
                                    "entity_aliases", "fact_entities", "build_info"]
    assert d.sink.content["facts"].columns[:3] == ("id", "subject_id", "statement")


def test_a_target_that_exists_is_refused_before_anything_is_read_or_written():
    files = FakeFiles(existing=["/out/x.db"])
    d = deps(files=files)
    with pytest.raises(NormalDbError, match="already exists"):
        svc.build_normal_db(d, "/full.db", "/out/x.db", privacy.Rules())
    assert d.sink.content is None


def test_foreign_key_violations_and_extra_objects_are_rejected():
    bad_fk = Integrity([(1, "facts", 1, 0)], {}, set(TABLES), 0)
    with pytest.raises(NormalDbError, match="foreign key"):
        svc.build_normal_db(deps(integrity=bad_fk), "/f", "/out/x.db", privacy.Rules())
    extra = Integrity([], {}, set(TABLES) | {"claims"}, 0)
    with pytest.raises(NormalDbError, match="unexpected objects"):
        svc.build_normal_db(deps(integrity=extra), "/f", "/out/x.db", privacy.Rules())
    trig = Integrity([], {}, set(TABLES), 1)
    with pytest.raises(NormalDbError, match="triggers"):
        svc.build_normal_db(deps(integrity=trig), "/f", "/out/x.db", privacy.Rules())


def test_a_leak_rejects_the_build_removes_the_temp_and_the_stale_normal_db():
    files = FakeFiles(existing=[FINAL])                      # a previous normal db is on disk
    scan = FakeScan(cells=[("facts.statement", "x Quillon Fernsby x")],
                    material=material(private_subject_names=[("Quillon Fernsby",)]))
    d = deps(files=files, scan=scan)
    with pytest.raises(NormalDbError, match="leak test failed"):
        svc.build_normal_atomic(d, "/full.db", DIR, privacy.Rules())
    assert files.files == set()                             # temp and stale final both gone
    assert not [c for c in files.calls if c[0] == "replace"]


def test_a_raw_bytes_only_leak_is_rejected_too():
    files = FakeFiles()
    files.raw = b"...zephyr-family-matters..."
    d = deps(files=files, scan=FakeScan(material=material(private_subject_names=[("zephyr-family-matters",)])))
    with pytest.raises(NormalDbError, match="raw file bytes"):
        svc.build_normal_atomic(d, "/full.db", DIR, privacy.Rules())
    assert files.files == set()


def test_a_failure_while_writing_also_removes_the_stale_normal_db():
    files = FakeFiles(existing=[FINAL])
    d = deps(files=files, fail=RuntimeError("disk full"))
    with pytest.raises(RuntimeError):
        svc.build_normal_atomic(d, "/full.db", DIR, privacy.Rules())
    assert files.files == set()


def test_success_replaces_the_target_and_returns_counts():
    files = FakeFiles(existing=[FINAL])
    d = deps(files=files)
    path, counts = svc.build_normal_atomic(d, "/full.db", DIR, privacy.Rules())
    assert path == FINAL and counts == d.sink.integrity.counts
    assert files.files == {FINAL}
    assert ("replace", f"{FINAL}.building-tok", FINAL) in files.calls


def test_leak_check_can_be_skipped_but_the_structure_check_cannot():
    scan = FakeScan(cells=[("facts.statement", "private-thing-xyz")], material=material(private_subject_names=[("private-thing-xyz",)]))
    path, _ = svc.build_normal_atomic(deps(scan=scan), "/f", DIR, privacy.Rules(), leak_check=False)
    assert path == FINAL
    extra = Integrity([], {}, set(TABLES) | {"vault_files"}, 0)
    with pytest.raises(NormalDbError):
        svc.build_normal_atomic(deps(integrity=extra), "/f", DIR, privacy.Rules(), leak_check=False)


def test_an_empty_or_blank_marker_is_refused_before_the_file_is_read():
    files = FakeFiles()
    d = deps(files=files)
    for bad in ([" "], {"label": ""}):
        with pytest.raises(ValueError, match="empty marker"):
            svc.scan(d, "/out/n.db", bad)
    assert files.calls == [] and d.leak_scan.cell_paths == []


def test_scan_reports_the_cell_and_derived_markers_cover_every_private_category():
    cells = [("facts.statement", "café ZOË visited")]
    d = deps(scan=FakeScan(cells=cells))
    assert [l for l, _m, _w in svc.scan(d, "/n.db", {"unicode": "Zoë"})] == ["unicode"]   # matching is case-folded
    assert svc.scan(d, "/n.db", {"absent": "nothing like it"}) == []
    assert [w for _l, _m, w in svc.scan(d, "/n.db", {"name": "café"})] == ["facts.statement"]
    m = material(private_facts=[(7, "secret statement text", None, None, None)],
                 claims=[(1, "a claim text", None)], vault_paths=[("Vault/x/y.md",)], file_hashes=[("a" * 12,)],
                 private_entities=[("Quillon Fernsby", "quillon fernsby", "quillon-fernsby", None, None)],
                 legit={"secret statement text"})
    markers = svc.derive_markers(deps(scan=FakeScan(material=m)), "/full.db")
    assert "claim 1 statement" in " ".join(markers)
    assert "vault path: Vault/x/y.md" in markers
    assert not any(k.startswith("private fact 7 statement") for k in markers)   # shareable and legit: not a marker
    assert any(k.startswith("private entity name") for k in markers)
