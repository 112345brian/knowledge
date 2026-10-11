"""entities_service / subjects_service against in-memory fake ports.

The services call entity_rules / subject_rules directly; the ports are only the file (read, atomic save) and
edit_file's locked git flow. The fake edit_file mirrors that flow (read, edit, refuse on a rules error, save
unless dry-run or unchanged), so these tests pin the validation messages, the privacy behavior and what the
services hand to the port.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import entities_service  # noqa: E402
import entity_rules  # noqa: E402
import subject_rules  # noqa: E402
import subjects_service  # noqa: E402
from locations import Locations  # noqa: E402
from private_edit_store import EditResult  # noqa: E402

LOC = Locations(knowledge_db_dir="/db", private_data_dir="/data", notes_vault="/vault", health_dir="/health")


class FakeGit:
    pass


class FakeFilePort:
    """An in-memory EntityFiles / SubjectFiles: one file, with edit_file's refuse/dry-run/changed semantics."""

    def __init__(self, error_type, content=None):
        self.error_type, self.content, self.saves, self.calls = error_type, content, [], []

    def read_file(self, path):
        return None if self.content is None else list(self.content)

    def save(self, items, path):
        self.content = list(items)
        self.saves.append(path)

    def edit_file(self, edit, what, message, allow_dirty=False, dry_run=False, *, path, git):
        self.calls.append(dict(what=what, message=message, allow_dirty=allow_dirty, dry_run=dry_run, path=path, git=git))
        try:
            new, changed = edit(self.read_file(path) or [])
        except self.error_type as e:
            return EditResult(False, what=what, message=message, errors=[str(e)], path=path)
        if changed and not dry_run:
            self.save(new, path)
        return EditResult(True, changed=changed, what=what, message=message, dry_run=dry_run, path=path)


class FakeMigration:
    def __init__(self):
        self.calls = []

    def migrate_keywords(self, allow_dirty=False, dry_run=False, keep_keywords=False, *, git, data_dir):
        self.calls.append(dict(allow_dirty=allow_dirty, dry_run=dry_run, keep_keywords=keep_keywords, data_dir=data_dir))
        return "migrated"


class FakeQueries:
    def entity_link_counts(self, db_path):
        return {"mary-jane": 2}

    def known_subjects(self, db_path):
        return {}

    def subject_rows(self, db_path):
        return []


def _entities(content=None):
    files, migration = FakeFilePort(entity_rules.EntitiesError, content), FakeMigration()
    deps = entities_service.EntitiesDeps(git=FakeGit(), entity_files=files, entity_migration=migration,
                                         entity_queries=FakeQueries(), locations=LOC)
    return deps, files, migration


def _subjects(content=None):
    files = FakeFilePort(subject_rules.SubjectsError, content)
    deps = subjects_service.SubjectsDeps(git=FakeGit(), subject_files=files, entity_queries=FakeQueries(), locations=LOC)
    return deps, files


# ------------------------------------------------------------------ entities

def test_the_ports_expose_only_io():
    import entity_files_port
    import subject_files_port
    members = lambda cls: {n for n in vars(cls) if not n.startswith("_")}  # noqa: E731
    assert members(entity_files_port.EntityFiles) == {"read_file", "save", "edit_file"}
    assert members(subject_files_port.SubjectFiles) == {"read_file", "save", "edit_file"}


def test_add_entity_goes_through_the_locked_flow_and_saves_the_rule_result():
    deps, files, _ = _entities()
    res = entities_service.add(deps, "Mary Jane", "person", ("MJ",), True, "wikidata:Q42", "n", allow_dirty=True)
    assert res.ok and res.changed
    assert files.calls[0]["what"] == "add entity 'Mary Jane' (private)"
    assert files.calls[0]["message"] == "entities: add Mary Jane"
    assert files.calls[0]["path"] == LOC.entities_file() and files.calls[0]["allow_dirty"] is True
    (e,) = files.content
    assert (e["id"], e["aliases"], e["private"], e["external_id"]) == ("mary-jane", ["MJ"], True, "wikidata:Q42")


def test_add_entity_keeps_the_validation_messages_and_writes_nothing():
    deps, files, _ = _entities()
    res = entities_service.add(deps, "X", "spaceship")
    assert not res.ok and "type 'spaceship' must be one of" in res.errors[0]
    assert files.saves == []
    entities_service.add(deps, "Mary Jane", "person", ("MJ",))
    res = entities_service.add(deps, "Other", "person", ("mj",))  # the alias collides after normalization
    assert not res.ok and files.content[0]["canonical_name"] == "Mary Jane" and len(files.content) == 1


def test_add_entity_dry_run_does_not_save():
    deps, files, _ = _entities()
    res = entities_service.add(deps, "Mary Jane", "person", dry_run=True)
    assert res.ok and res.changed and res.dry_run and files.saves == [] and files.content is None


def test_alias_adds_refuses_unknown_and_is_idempotent():
    deps, files, _ = _entities()
    entities_service.add(deps, "Mary Jane", "person")
    assert entities_service.alias(deps, "mary-jane", "MJ").changed
    assert files.content[0]["aliases"] == ["MJ"]
    assert files.calls[-1]["message"] == "entities: alias MJ -> mary-jane"
    assert files.calls[-1]["what"] == "add alias 'MJ' to mary-jane"
    again = entities_service.alias(deps, "mj", "mj")  # already a term of the entity
    assert again.ok and not again.changed
    bad = entities_service.alias(deps, "nobody", "N")
    assert not bad.ok and bad.errors == ["unknown entity 'nobody'"]


def test_tag_and_untag_set_the_privacy_flag_that_the_resolver_reads():
    deps, files, _ = _entities()
    entities_service.add(deps, "Mary Jane", "person", ("MJ",))
    assert not entity_rules.private_terms(files.content)
    res = entities_service.tag(deps, "MJ")
    assert res.changed and files.calls[-1]["what"] == "tag entity MJ private"
    assert files.calls[-1]["message"] == "entities: tag MJ private"
    assert files.content[0]["private"] is True
    assert entity_rules.private_terms(files.content) == (("Mary Jane", ("mary jane", "mj")),)  # what the resolver floors on
    assert not entities_service.tag(deps, "MJ").changed  # already private
    res = entities_service.untag(deps, "mary-jane")
    assert res.changed and files.calls[-1]["what"] == "untag entity mary-jane"
    assert files.calls[-1]["message"] == "entities: untag mary-jane"
    assert not entity_rules.private_terms(files.content)
    assert entities_service.untag(deps, "nobody").errors == ["unknown entity 'nobody'"]


def test_migrate_keywords_passes_everything_to_the_migration_port():
    deps, files, migration = _entities()
    out = entities_service.migrate_keywords(deps, allow_dirty=True, dry_run=True, keep_keywords=True)
    assert out == "migrated"
    assert migration.calls == [dict(allow_dirty=True, dry_run=True, keep_keywords=True, data_dir=LOC.data_dir)]
    assert files.calls == []


def test_load_find_and_link_counts():
    seed = entity_rules.add_entity([], "Mary Jane", "person", ("MJ",))[0]
    deps, _, _ = _entities(seed)
    assert entities_service.load_entities(deps) == seed
    assert entities_service.find(deps, seed, "mj")["id"] == "mary-jane"
    assert entities_service.find(deps, seed, "nobody") is None
    assert entities_service.link_counts(deps) == {"mary-jane": 2}
    assert entities_service.load_entities(_entities()[0]) == []


# ------------------------------------------------------------------ subjects

def test_subject_alias_describe_deprecate_use_the_rules_and_pass_domain_and_known():
    deps, files = _subjects()
    known = {"topic": "example", "other": "example"}
    res = subjects_service.alias(deps, "topic", "alt", known)
    assert res.ok and res.changed and files.calls[-1]["what"] == "add alias alt to topic"
    assert files.calls[-1]["message"] == "subjects: alias alt -> topic"
    assert files.calls[-1]["path"] == LOC.subjects_file()
    assert files.content[0]["domain"] == "example" and files.content[0]["aliases"] == ["alt"]
    res = subjects_service.describe(deps, "topic", "  A scope note  ", known)
    assert res.changed and files.content[0]["description"] == "A scope note"
    assert files.calls[-1]["message"] == "subjects: describe topic"
    res = subjects_service.deprecate(deps, "topic", "other", known)
    assert res.changed and files.calls[-1]["what"] == "deprecate topic (use other)"
    assert files.calls[-1]["message"] == "subjects: deprecate topic"
    by_name = {e["name"]: e for e in files.content}
    assert by_name["topic"]["deprecated"] and by_name["topic"]["replaced_by"] == "other" and "other" in by_name


def test_subject_validation_messages_are_preserved_and_nothing_is_written():
    deps, files = _subjects()
    known = {"topic": "example"}
    assert subjects_service.alias(deps, "nope", "alt", known).errors == ["unknown subject 'nope'"]
    assert subjects_service.alias(deps, "topic", "Bad Alias", known).errors == [
        "alias 'Bad Alias' must be a lowercase kebab-case slug"]
    assert subjects_service.alias(deps, "topic", "topic", known).errors == [
        "'topic' is already a subject name; an alias may not shadow a subject"]
    assert subjects_service.deprecate(deps, "topic", "topic", known).errors == ["a subject cannot replace itself"]
    assert subjects_service.deprecate(deps, "topic", "ghost", known).errors == ["unknown replacement subject 'ghost'"]
    assert subjects_service.describe(deps, "nope", "x", known).errors == ["unknown subject 'nope'"]
    assert files.saves == []


def test_subject_edits_are_idempotent():
    deps, files = _subjects()
    known = {"topic": "example"}
    subjects_service.alias(deps, "topic", "alt", known)
    subjects_service.deprecate(deps, "topic", None, known)
    n = len(files.saves)
    assert not subjects_service.alias(deps, "topic", "alt", known).changed
    assert not subjects_service.deprecate(deps, "topic", None, known).changed
    assert len(files.saves) == n


def test_subject_detail_and_alias_owner_lookup_use_the_query_port():
    class Q(FakeQueries):
        def subject_rows(self, db_path):
            return [dict(name="a", parent=None, aliases=["x"]), dict(name="b", parent="a", aliases=[])]
    deps, _ = _subjects()
    deps = subjects_service.SubjectsDeps(git=deps.git, subject_files=deps.subject_files, entity_queries=Q(),
                                         locations=LOC)
    d = subjects_service.detail(deps, "x")
    assert d["name"] == "a" and d["resolved_from_alias"] and d["children"] == ["b"]
    assert subjects_service.detail(deps, "zzz") is None
