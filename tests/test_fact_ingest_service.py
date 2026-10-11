"""The fact ingest use case against fakes of its ports: no database, no file, no clock.

`fact_ingest_service` is handed an in-memory repository, inputs and privacy floor. What it decides (screening,
key checks, duplicates, freshness, provenance, the implicit revision, the order of the writes) is asserted on the
fake's log and on the domain records it returns.
"""
import ast
import dataclasses
import os

import pytest

import fact_ingest_decisions as decisions
import fact_ingest_service as svc
from appctx import synthetic_locations
from revisions import RevisionError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = synthetic_locations().private_data_dir
VAULT = "/vault"


class FakeRepo:
    def __init__(self, sources=None, existing_keys=()):
        self.log = []
        self.sources = dict(sources or {})
        self.keys = set(existing_keys)
        self.subjects = {}
        self.vault = {}            # path -> [id, fingerprinted]
        self.facts = []            # FactRow, in insert order
        self.revisions = []
        self.links = []
        self.committed = 0

    def subject_id(self, con, name, domain=None, resolve_aliases=False):
        self.log.append(("subject", name, domain, resolve_aliases))
        return self.subjects.setdefault(name, len(self.subjects) + 1)

    def source_ids_by_citekey(self, con):
        return dict(self.sources)

    def fact_exists(self, con, source_key):
        return source_key in self.keys

    def find_vault_file(self, con, path):
        row = self.vault.get(path)
        return None if row is None else tuple(row)

    def add_vault_file(self, con, path):
        self.vault[path] = [len(self.vault) + 1, False]
        return self.vault[path][0]

    def record_vault_file(self, con, file_id, fingerprint, acquisition):
        self.log.append(("fingerprint", file_id, fingerprint, acquisition))
        for row in self.vault.values():
            if row[0] == file_id:
                row[1] = True

    def insert_fact(self, con, row):
        self.log.append(("fact", row.source_key))
        self.facts.append(row)
        self.keys.add(row.source_key)
        return len(self.facts)

    def insert_revision(self, con, fact_id, revision):
        self.revisions.append((fact_id, revision))

    def link_source(self, con, fact_id, source_id, locator, quote):
        self.links.append((fact_id, source_id, locator, quote))

    def facts_by(self, con, column):
        out = {}
        for f in self.facts:
            out[getattr(f, column)] = out.get(getattr(f, column), 0) + 1
        return out

    def commit(self, con):
        self.log.append(("commit",))
        self.committed += 1


class FakeInputs:
    def __init__(self, files):
        self.files = files
        self.fingerprinted = []

    def read_json(self, path):
        name = os.path.basename(path)
        if name not in self.files:
            raise FileNotFoundError(path)
        return self.files[name]

    def fingerprint(self, path):
        self.fingerprinted.append(path)
        return {"content_sha256": "f" * 64, "size_bytes": 1, "file_mtime": "t", "mime_type": "text/markdown",
                "file_state": "present"}

    def acquisition(self, row, path):
        return dict(row)


class FakePrivacy:
    def __init__(self, repo, raised=()):
        self.repo, self.raised, self.loaded, self.applied = repo, list(raised), [], []

    def load_privacy_rules(self, path):
        self.repo.log.append(("rules-loaded",))
        self.loaded.append(path)
        return "rules"

    def apply_privacy_rules(self, con, rules):
        self.repo.log.append(("privacy-applied",))
        self.applied.append(rules)
        return {"raised": self.raised, "private_subjects": 0}


def world(files, sources=None, existing_keys=(), raised=(), vault=VAULT):
    repo = FakeRepo(sources, existing_keys)
    locations = dataclasses.replace(synthetic_locations(), notes_vault=vault)
    inputs = FakeInputs(files)
    privacy = FakePrivacy(repo, raised)
    deps = svc.FactIngestDeps(facts=repo, inputs=inputs, privacy_floor=privacy, locations=locations)
    return deps, repo, inputs, privacy


def entry(**kw):
    base = dict(subject="coffee", statement="A statement.", trust_level="low", date_added="2026-01-02",
                freshness="no-decay", recheck_rationale="stable", source_key="k-1")
    base.update(kw)
    return base


FACT_FILES = {n: [] for n in decisions.FACT_FILENAMES}


def fact_files(pilot=(), **others):
    return {**FACT_FILES, "pilot_facts.json": list(pilot), **others}


# ------------------------------------------------------------------ accepted, skipped, ordered

def test_accepted_and_skipped_entries_come_back_as_domain_records():
    deps, repo, _, _ = world({"general_facts.json": [
        entry(source_key="a", statement="Kept."),
        entry(source_key="b", statement="Bad vis.", visibility="public"),
        entry(source_key="c", statement=""),
        entry(source_key="d", statement="Kept too.", subject="tea"),
    ]})
    report = svc.ingest_general_facts(deps, None)
    assert [(i.source_key, i.fact_id, i.subject_id) for i in report.ingested] == [("a", 1, 1), ("d", 2, 2)]
    assert [s.warning is None for s in report.skipped] == [False, True]
    assert report.skipped[0].warning.startswith("  WARNING -- skipping fact with invalid visibility 'public'")
    assert [f.statement for f in repo.facts] == ["Kept.", "Kept too."]
    assert [r["source_key"] for _, r in repo.revisions] == ["a", "d"] and all(r["revision"] == 1 for _, r in repo.revisions)


def test_the_privacy_floor_is_loaded_first_applied_after_the_last_insert_and_then_committed():
    deps, repo, _, privacy = world({"general_facts.json": [entry(source_key="a"), entry(source_key="b", statement="B.")]},
                                   raised=[(1, "x")])
    report = svc.ingest_general_facts(deps, None)
    kinds = [e[0] for e in repo.log]
    assert kinds[0] == "rules-loaded"
    assert kinds[-3:] == ["fact", "privacy-applied", "commit"]
    assert privacy.loaded == [deps.locations.privacy_rules()] and report.privacy_raised == 1


def test_a_corrupt_rules_file_fails_before_anything_is_written():
    deps, repo, _, privacy = world({"general_facts.json": [entry()]})
    privacy.load_privacy_rules = lambda path: (_ for _ in ()).throw(ValueError("corrupt rules"))
    with pytest.raises(ValueError, match="corrupt rules"):
        svc.ingest_general_facts(deps, None)
    assert repo.facts == [] and repo.committed == 0


# ------------------------------------------------------------------ build errors

def test_a_duplicate_source_key_fails_the_build():
    deps, repo, _, _ = world({"general_facts.json": [entry(source_key="same"), entry(source_key="same", statement="Other.")]})
    with pytest.raises(ValueError, match="duplicate source_key 'same'"):
        svc.ingest_general_facts(deps, None)


def test_a_key_already_in_the_database_is_a_duplicate():
    deps, _, _, _ = world({"general_facts.json": [entry(source_key="taken")]}, existing_keys={"taken"})
    with pytest.raises(ValueError, match="duplicate source_key 'taken'"):
        svc.ingest_general_facts(deps, None)


@pytest.mark.parametrize("bad,match", [
    (dict(source_key="Not A Key"), "invalid source_key"),
    (dict(extracted_from_sha256="abc"), "invalid extracted_from_sha256"),
    (dict(date_added=None), "invalid `date_added`"),
    (dict(freshness=None, recheck_rationale=None), "no `freshness`"),
    (dict(kind="bogus"), "kind"),
    (dict(valid_from="2026-13-40"), "valid_from|valid"),
])
def test_a_malformed_entry_fails_the_build_not_a_skip(bad, match):
    deps, repo, _, _ = world({"general_facts.json": [entry(**bad)]})
    with pytest.raises((ValueError, RevisionError), match=match):
        svc.ingest_general_facts(deps, None)
    assert repo.facts == [] and repo.committed == 0


# ------------------------------------------------------------------ what each profile decides

def test_general_facts_resolve_aliases_default_the_domain_and_take_their_own_personal_flag():
    deps, repo, _, _ = world({"general_facts.json": [entry(source_key="a", domain="food"), entry(source_key="b", statement="B.", is_personal=False)]})
    svc.ingest_general_facts(deps, None)
    assert repo.log[1] == ("subject", "coffee", "food", True)
    assert [f.is_personal for f in repo.facts] == [1, 0]
    assert repo.log.count(("subject", "coffee", "food", True)) == 1   # the subject is looked up once per name


def test_fact_files_do_not_resolve_aliases_and_derive_the_personal_flag_and_origin_note():
    note = f"{VAULT}/Health/Sleep.md"
    deps, repo, inputs, _ = world(fact_files(pilot=[
        entry(source_key="a", statement="He sleeps eight hours.", notes=f"{note}, extracted"),
        entry(source_key="b", statement="Water is wet.", notes="nothing"),
    ]))
    svc.ingest_fact_files(deps, None)
    assert ("subject", "coffee", None, False) in repo.log
    assert [f.is_personal for f in repo.facts] == [1, 0]
    assert [f.origin_file_id for f in repo.facts] == [1, None]
    assert inputs.fingerprinted == [note]


def test_a_vault_file_cited_by_many_facts_is_fingerprinted_once():
    note = f"{VAULT}/n.md"
    deps, repo, inputs, _ = world({"general_facts.json": [entry(source_key=f"k-{i}", statement=f"S{i}.", origin_path=note)
                                                           for i in range(4)]})
    svc.ingest_general_facts(deps, None)
    assert inputs.fingerprinted == [note] and {f.origin_file_id for f in repo.facts} == {1}


def test_general_facts_do_not_derive_an_origin_note_from_notes():
    deps, repo, inputs, _ = world({"general_facts.json": [entry(notes=f"{VAULT}/x.md, y")]})
    svc.ingest_general_facts(deps, None)
    assert inputs.fingerprinted == [] and repo.facts[0].origin_file_id is None


def test_a_legacy_entry_gets_unreviewed_freshness_and_its_note_a_current_one_keeps_its_own():
    legacy = {"subject": "s", "statement": "Old.", "trust_level": "low", "date_added": "2026-01-02"}
    deps, repo, _, _ = world(fact_files(pilot=[legacy, entry(source_key="new", captured_via="cli")]))
    svc.ingest_fact_files(deps, None)
    old, new = repo.facts
    assert old.freshness == "unreviewed" and "predates the freshness field" in old.notes
    assert new.freshness == "no-decay" and new.captured_via == "cli"
    assert old.source_key.startswith("legacy-")


def test_the_implicit_revision_carries_the_entry_provenance():
    deps, repo, _, _ = world({"general_facts.json": [entry(captured_via="mcp", session_id="s-1", kind="decision",
                                                           valid_from="2026", applies_to=" adults ")]})
    svc.ingest_general_facts(deps, None)
    (fact_id, rev), = repo.revisions
    assert (fact_id, rev["source_key"], rev["revision"], rev["changed_via"], rev["session_id"], rev["kind"]) == \
        (1, "k-1", 1, "mcp", "s-1", "decision")
    assert (rev["valid_from"], rev["applies_to"], rev["changed_at"]) == ("2026", "adults", "2026-01-02")
    row = repo.facts[0]
    assert (row.kind, row.valid_from, row.applies_to, row.last_reviewed_at) == ("decision", "2026", "adults", "2026-01-02")


# ------------------------------------------------------------------ sources and keys

def test_a_known_citekey_is_linked_and_an_unknown_one_is_reported_not_linked():
    deps, repo, _, _ = world({"general_facts.json": [
        entry(source_key="a", source_citekey="known", source_locator="p. 3", source_quote="q"),
        entry(source_key="b", statement="B.", source_citekey="ghost"),
    ]}, sources={"known": 7})
    report = svc.ingest_general_facts(deps, None)
    assert repo.links == [(1, 7, "p. 3", "q")]
    assert report.unknown_citekeys == {"ghost"} and [i.source_id for i in report.ingested] == [7, None]


def test_entries_without_their_own_key_count_as_derived():
    bare = {"subject": "s", "statement": "One.", "trust_level": "low", "date_added": "2026-01-02",
            "freshness": "no-decay", "recheck_rationale": "r"}
    deps, _, _, _ = world({"general_facts.json": [bare, dict(bare, statement="Two."), entry(source_key="own")]})
    assert svc.ingest_general_facts(deps, None).derived_keys == 2


def test_load_fact_file_entries_reads_every_file_in_order_with_keys_and_pilot_flag():
    deps, _, _, _ = world(fact_files(pilot=[entry(source_key="p-1")], **{"facts_batch2.json": [entry(source_key="b-1")]}))
    refs = svc.load_fact_file_entries(deps)
    assert [(r.file, r.index, r.key, r.is_pilot) for r in refs] == [("pilot_facts.json", 0, "p-1", True), ("facts_batch2.json", 0, "b-1", False)]


# ------------------------------------------------------------------ the report

def test_the_report_renders_the_lines_the_steps_print():
    deps, _, _, _ = world(fact_files(pilot=[entry(source_key="a", source_citekey="ghost"),
                                            entry(source_key="b", statement="B.", visibility="public")]), raised=[(1, "x")])
    lines = decisions.render(svc.ingest_fact_files(deps, None))
    assert lines[0].startswith("  WARNING -- skipping fact with invalid visibility")
    assert lines[1:] == [
        "  privacy rules raised 1 fact(s) to private",
        "[facts] inserted 1, skipped 1 (bad shape)",
        "  WARNING -- citekeys referenced but not found in sources: ['ghost']",
        "  facts by freshness: {'no-decay': 1}",
        "  facts by trust_level: {'low': 1}",
        "  facts by is_personal: {0: 1}",
    ]


def test_the_general_report_has_no_breakdowns():
    deps, _, _, _ = world({"general_facts.json": [entry()]})
    assert decisions.render(svc.ingest_general_facts(deps, None)) == ["[seed_general_facts] inserted 1, skipped 0 (bad shape)"]


# ------------------------------------------------------------------ one implementation, results read

def _parse(name):
    with open(os.path.join(REPO, name), encoding="utf-8") as f:
        return ast.parse(f.read())


def test_both_steps_run_the_same_decisions_through_one_function(monkeypatch):
    seen = []
    real = svc.ingest_entries
    monkeypatch.setattr(svc, "ingest_entries", lambda deps, con, entries, profile, *a: seen.append(profile.label) or real(deps, con, entries, profile, *a))
    d1, *_ = world(fact_files(pilot=[entry()]))
    d2, *_ = world({"general_facts.json": [entry()]})
    svc.ingest_fact_files(d1, None)
    svc.ingest_general_facts(d2, None)
    assert seen == ["facts", "seed_general_facts"]


@pytest.mark.parametrize("step", ["ingest/facts.py", "ingest/seed_general_facts.py"])
def test_a_step_holds_no_decision_and_no_sql_of_its_own(step):
    tree = _parse(step)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
               {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"revisions", "fixity_rules", "privacy", "sqlite3", "ingest.fact_ingest_rules", "ingest.shared"}, imported
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("execute", "executemany", "cursor")]


def test_no_caller_discards_an_ingest_report():
    """A step that called `ingest_fact_files(...)` and dropped the report would print nothing and hide a skipped fact."""
    offenders = []
    for folder in ("ingest", "client"):
        for name in sorted(os.listdir(os.path.join(REPO, folder))):
            if not name.endswith(".py"):
                continue
            for node in ast.walk(_parse(os.path.join(folder, name))):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    f = node.value.func
                    called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if called in ("ingest_fact_files", "ingest_general_facts", "ingest_entries"):
                        offenders.append(f"{folder}/{name}:{node.lineno}")
    assert offenders == [], offenders
