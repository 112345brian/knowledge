"""`fact_ingest.BuildIdentity`: the fact ingest's identity port, answered by the build's one definition of a subject
and a vault file (subject_hierarchy_service, source_ingest_service) over the real SQLite repositories."""
import os
import sqlite3

import pytest

import fact_ingest
import source_repository_store
import subject_repository_store

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Inputs:
    def __init__(self):
        self.fingerprinted = []

    def fingerprint(self, path):
        self.fingerprinted.append(path)
        return {"content_sha256": "b" * 64, "size_bytes": 3, "file_mtime": "t", "mime_type": "text/markdown",
                "file_state": "present"}

    def acquisition(self, row, path):
        return dict(row, where_from="https://x", acquired_note="n")


@pytest.fixture
def world():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    inputs = Inputs()
    identity = fact_ingest.BuildIdentity(subject_repository_store.SqliteSubjects(con),
                                         source_repository_store.SqliteSourceRepository(con), inputs)
    return con, identity, inputs


def test_a_subject_is_created_once_and_found_again(world):
    con, identity, _ = world
    a = identity.subject_id("coffee")
    assert identity.subject_id("coffee") == a
    assert con.execute("SELECT COUNT(*) FROM subjects WHERE name = 'coffee'").fetchone()[0] == 1


def test_a_new_subject_takes_the_domain_it_is_given_else_the_schema_default(world):
    con, identity, _ = world
    default = con.execute("SELECT domain FROM subjects WHERE id = ?", (identity.subject_id("plain"),)).fetchone()[0]
    given = con.execute("SELECT domain FROM subjects WHERE id = ?", (identity.subject_id("other", "general"),)).fetchone()[0]
    assert given == "general" and default != "general"


def test_an_alias_resolves_to_its_subject_only_when_asked(world):
    con, identity, _ = world
    canonical = identity.subject_id("coffee")
    con.execute("INSERT INTO subject_aliases (alias, subject_id) VALUES ('java', ?)", (canonical,))
    assert identity.subject_id("java", None, True) == canonical
    # without it the name is looked up as a subject and, being a taken alias, cannot be created (as before)
    with pytest.raises(sqlite3.IntegrityError):
        identity.subject_id("java")


def test_a_vault_file_is_recorded_with_its_fixity_once(world):
    con, identity, inputs = world
    first = identity.vault_file_id("/n.md")
    assert identity.vault_file_id("/n.md") == first and inputs.fingerprinted == ["/n.md"]
    got = con.execute("SELECT content_sha256, size_bytes, file_state, where_from, acquired_note FROM vault_files").fetchone()
    assert tuple(got) == ("b" * 64, 3, "present", "https://x", "n")
    assert identity.vault_file_id(None) is None
