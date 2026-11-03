"""The facts file's label in `add-fact`'s confirmation message must never fail the command.

`add_fact.data_file_label` is shown as "Added to <label>"; the fact is already written by then. On Windows the data file
can be on another drive than the repo (data in a sync folder on `E:`, code on `C:`), and `os.path.relpath` raises for
that. Its one caller is `cli_add_fact.cmd_add_fact`.
"""
import os

import add_fact


def test_a_file_in_the_repo_is_shown_relative_to_it():
    here = os.path.join(os.sep, "repo")
    assert add_fact.data_file_label(os.path.join(here, "data", "general_facts.json"), here) == os.path.join("data", "general_facts.json")


def test_a_file_elsewhere_is_shown_relative_when_a_relative_form_exists():
    here = os.path.join(os.sep, "repo")
    assert add_fact.data_file_label(os.path.join(os.sep, "elsewhere", "facts.json"), here) == os.path.join("..", "elsewhere", "facts.json")


def test_a_file_with_no_relative_form_is_shown_by_its_absolute_path_instead_of_raising(monkeypatch):
    """Windows: a path on another drive has no relative form and os.path.relpath raises ValueError."""
    def another_drive(path, start):
        raise ValueError(f"path is on mount 'C:', start on mount 'D:'")
    monkeypatch.setattr(os.path, "relpath", another_drive)
    assert add_fact.data_file_label("C:/data/general_facts.json", "D:/repo") == "C:/data/general_facts.json"


def test_the_composed_api_uses_the_same_label(monkeypatch):
    import clock
    from locations import Locations
    loc = Locations(knowledge_db_dir="/db", private_data_dir="/data")
    api = add_fact.compose(loc, clock.SystemClock())
    monkeypatch.setattr(os.path, "relpath", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("other drive")))
    assert api.data_file_label() == loc.data_path
