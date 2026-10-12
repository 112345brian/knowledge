"""What each caller of the subjects repository relies on, pinned over the real SQLite adapter and the real schema.

The callers are `subject_hierarchy_service.load_hierarchy` (step seed_subject_hierarchy), `subject_for_fact` (through
`fact_ingest.BuildIdentity`), and `identity_service.SubjectIdentity` (the measurements step). Every test goes through
the services, so it holds whichever port shape the adapter implements; the edge inputs are the ones a collapse onto
the foundation port could quietly change (blank and NUL names, a partial entry, a failure on a later alias).
"""
import os
import pathlib
import re
import sqlite3

import pytest

import identity_service
import subject_hierarchy_service as svc
import subject_repository_store

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(open(os.path.join(REPO, "schema.sql"), encoding="utf-8").read())
    return c


@pytest.fixture
def deps(con):
    return svc.HierarchyDeps(subjects=subject_repository_store.SqliteSubjects(con))


def entry(name, **kw):
    return {"name": name, "domain": None, "parent": None, "relation": None, "description": None, "aliases": [],
            "deprecated": False, "replaced_by": None, **kw}


def rows(con):
    return [tuple(r) for r in con.execute("SELECT id, name, domain, parent_id, parent_relation, description, "
                                          "deprecated, replaced_by_subject_id FROM subjects ORDER BY id")]


# ------------------------------------------------------------------ load_hierarchy

def test_load_allocates_ids_in_file_order_and_leaves_the_default_domain(con, deps):
    svc.load_hierarchy(deps, [entry("zeta"), entry("alpha", domain="music"), entry("mid", parent="zeta")])
    assert [(r[0], r[1], r[2]) for r in rows(con)] == [(1, "zeta", "health-and-fitness"), (2, "alpha", "music"),
                                                       (3, "mid", "health-and-fitness")]
    assert rows(con)[2][3] == 1 and rows(con)[2][4] == "broader"


def test_a_second_load_of_the_same_entries_changes_nothing_but_refuses_the_alias(con, deps):
    entries = [entry("a", aliases=["ay"])]
    svc.load_hierarchy(deps, entries)
    before = rows(con)
    with pytest.raises(ValueError, match="alias 'ay' of 'a' cannot be loaded"):
        svc.load_hierarchy(deps, entries)          # the alias is already claimed: the build fails loudly
    assert rows(con) == before


def test_a_failure_on_the_second_alias_keeps_what_was_claimed_before_it(con, deps):
    with pytest.raises(ValueError, match="alias 'b' of 'a' cannot be loaded"):
        svc.load_hierarchy(deps, [entry("a", aliases=["x", "b"]), entry("b")])
    # 'x' (listed first) was claimed and stays: the step commits only after a clean load, the error aborts the build
    assert [r[0] for r in con.execute("SELECT alias FROM subject_aliases")] == ['x']


def test_alias_equal_to_its_own_subject_name_is_refused(con, deps):
    with pytest.raises(ValueError, match=r"alias 'a' of 'a' cannot be loaded \(alias collides with a subject name\)"):
        svc.load_hierarchy(deps, [entry("a", aliases=["a"])])


def test_two_entries_claiming_one_alias_name_the_first_owner(con, deps):
    with pytest.raises(ValueError, match=r"alias 'dup' of 'two' cannot be loaded \(alias already belongs to 'one'\)"):
        svc.load_hierarchy(deps, [entry("one", aliases=["dup"]), entry("two", aliases=["dup"])])


def test_blank_and_nul_names_are_stored_as_given(con, deps):
    svc.load_hierarchy(deps, [entry(""), entry("a\x00b")])
    assert [r[1] for r in rows(con)] == ["", "a\x00b"]


def test_a_replacement_listed_after_the_deprecated_subject(con, deps):
    t = svc.load_hierarchy(deps, [entry("old", deprecated=True, replaced_by="new"), entry("new")])
    r = {x[1]: x for x in rows(con)}
    assert r["old"][6] == 1 and r["old"][7] == r["new"][0]
    assert (t.with_parent, t.aliases, t.deprecated) == (0, 0, 1)


def test_a_partial_entry_of_names_only_loads_with_defaults(con, deps):
    svc.load_hierarchy(deps, [{"name": "bare", "domain": None, "parent": None, "relation": None, "description": None,
                               "aliases": (), "deprecated": None, "replaced_by": None}])
    assert rows(con) == [(1, "bare", "health-and-fitness", None, "broader", None, 0, None)]


def test_the_tally_counts_the_whole_table_not_only_this_load(con, deps):
    con.execute("INSERT INTO subjects (name) VALUES ('p')")
    con.execute("INSERT INTO subjects (name, parent_id, deprecated) VALUES ('c', 1, 1)")
    con.execute("INSERT INTO subject_aliases (alias, subject_id) VALUES ('pp', 1)")
    t = svc.load_hierarchy(deps, [])
    assert (t.with_parent, t.aliases, t.deprecated) == (1, 1, 1)


# ------------------------------------------------------------------ subject_for_fact

def test_subject_for_fact_without_resolving_aliases_ignores_the_alias_table(con, deps):
    svc.load_hierarchy(deps, [entry("coffee", aliases=["java"])])
    canonical = con.execute("SELECT id FROM subjects WHERE name = 'coffee'").fetchone()[0]
    assert svc.subject_for_fact(deps, "java", resolve_aliases=True) == canonical
    with pytest.raises(sqlite3.IntegrityError):         # a taken alias cannot become a subject (trigger)
        svc.subject_for_fact(deps, "java", resolve_aliases=False)


def test_subject_for_fact_blank_and_nul_names_create_a_subject_each(con, deps):
    a = svc.subject_for_fact(deps, "")
    b = svc.subject_for_fact(deps, "x\x00y", "general")
    assert svc.subject_for_fact(deps, "") == a and a != b
    assert con.execute("SELECT domain FROM subjects WHERE id = ?", (b,)).fetchone()[0] == "general"


def test_subject_for_fact_cache_is_keyed_by_the_name_as_given(con, deps):
    cache = {}
    first = svc.subject_for_fact(deps, "x", "one", cache)
    assert svc.subject_for_fact(deps, "x", "two", cache) == first
    assert cache == {"x": first}
    assert con.execute("SELECT domain FROM subjects WHERE id = ?", (first,)).fetchone()[0] == "one"


# ------------------------------------------------------------------ SubjectIdentity (the foundation resolve)

def test_subject_identity_over_the_real_adapter_matches_subject_for_fact(con, deps):
    svc.load_hierarchy(deps, [entry("coffee", aliases=["java"])])
    ident = identity_service.SubjectIdentity(deps.subjects)
    canonical = svc.subject_for_fact(deps, "coffee")
    assert ident.resolve("java", via_aliases=True) == canonical
    assert ident.resolve("tea", domain="general") == svc.subject_for_fact(deps, "tea")
    assert ident.touched == 2


# ------------------------------------------------------------------ nobody else writes subjects

def test_only_the_subjects_adapter_writes_the_subject_tables():
    """Subject rows are written by `subject_repository_store` alone (the schema, its fixtures and the normal-db
    exporter's own database aside); a second writer would be a second definition of identity."""
    allowed = {"subject_repository_store.py", "schema.sql"}
    pattern = re.compile(r"(INSERT\s+(OR\s+\w+\s+)?INTO\s+(subjects|subject_aliases)\b|UPDATE\s+subjects\s+SET)", re.I)
    offenders = []
    for path in sorted(pathlib.Path(REPO).glob("*.py")) + sorted(pathlib.Path(REPO, "ingest").glob("*.py")) \
            + sorted(pathlib.Path(REPO, "client").glob("*.py")):
        if path.name in allowed:
            continue
        if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(path.relative_to(REPO).as_posix())
    # the known others are outside this aggregate (listed in the PR body); a NEW writer fails this test
    assert offenders == sorted(offenders)
    assert set(offenders) <= KNOWN_OTHER_WRITERS, sorted(set(offenders) - KNOWN_OTHER_WRITERS)


KNOWN_OTHER_WRITERS = {"privacy_store.py",         # privacy aggregate: tags subjects `private`
                       "leak_test.py"}            # builds its own fixture database
