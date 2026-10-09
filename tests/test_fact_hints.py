"""The ingest knows nothing about the author's vault: what it learns from `fact_hints.json` in the private data dir
(fingerprints of facts about the owner, notes whose original claims are about the owner, note folders, titles with a
comma). With no such file only the generic pronoun rule applies."""
import json
import os
import re

import pytest

from ingest import fact_ingest_rules as rules
from ingest.fact_ingest_rules import Hints, NO_HINTS, parse_hints

HINTS = parse_hints({
    "fingerprints": [r"FFMI 15\.75", "knee surgery"],
    "personal_notes": ["Current State", "Goal Progress"],
    "note_folders": {"harm-reduction": ["Risk Notes.md"], "plans": ["Plan A.md", "Plan B.md"]},
    "whole_titles": ["Sets, Reps and Rest.md"],
})
VAULT = "/v"


# ------------------------------------------------------------------ classify

def test_the_generic_pronoun_rule_needs_no_hints():
    assert rules.classify_is_personal("He lifted heavier.", None, False, None) == 1
    assert rules.classify_is_personal("The vault owner slept badly.", None, False, None) == 1
    assert rules.classify_is_personal("Creatine is well studied.", "Some Note.md", False, None) == 0


def test_a_measured_link_is_personal_without_hints():
    assert rules.classify_is_personal("Weight.", None, False, "weight_lb") == 1


def test_without_hints_a_fingerprint_or_a_personal_note_means_nothing():
    assert rules.classify_is_personal("Had FFMI 15.75 in 2025.", None, False, None) == 0
    assert rules.classify_is_personal("Claim.", "Current State.md, x", True, None) == 0


def test_fingerprints_are_case_insensitive_regexes_over_statement_and_notes():
    assert rules.classify_is_personal("FFMI 15.75 measured.", None, False, None, HINTS) == 1
    assert rules.classify_is_personal("Recovered from KNEE SURGERY.", None, False, None, HINTS) == 1
    assert rules.classify_is_personal("Claim.", "after knee surgery", False, None, HINTS) == 1
    assert rules.classify_is_personal("FFMI 15x75", None, False, None, HINTS) == 0           # the dot is escaped in the pattern


def test_a_personal_note_counts_only_for_an_original_claim():
    assert rules.classify_is_personal("Claim.", "Current State.md, more", True, None, HINTS) == 1
    assert rules.classify_is_personal("Claim.", "Current State.md, more", False, None, HINTS) == 0
    assert rules.classify_is_personal("Claim.", "Other.md", True, None, HINTS) == 0


# ------------------------------------------------------------------ resolve_origin_path

def test_a_path_inside_the_vault_is_returned_as_written():
    assert rules.resolve_origin_path("/v/Notes/A.md, x", VAULT) == "/v/Notes/A.md"


def test_a_note_with_its_folder_resolves_without_hints():
    assert rules.resolve_origin_path("harm-reduction/Risk Notes.md, x", VAULT) == "/v/harm-reduction/Risk Notes.md"
    assert rules.resolve_origin_path("Risk Notes.md, x", VAULT) == "/v/Risk Notes.md"      # no hint, so no folder is guessed


def test_hints_name_the_folder_of_a_note_written_without_it():
    assert rules.resolve_origin_path("Risk Notes.md, x", VAULT, HINTS) == "/v/harm-reduction/Risk Notes.md"
    assert rules.resolve_origin_path("Plan B.md", VAULT, HINTS) == "/v/plans/Plan B.md"
    assert rules.resolve_origin_path("Unlisted.md", VAULT, HINTS) == "/v/Unlisted.md"


def test_a_title_with_a_comma_needs_the_hint():
    assert rules.resolve_origin_path("Sets, Reps and Rest.md", VAULT) is None
    assert rules.resolve_origin_path("Sets, Reps and Rest.md", VAULT, HINTS) == "/v/Sets, Reps and Rest.md"


def test_nothing_to_resolve():
    assert rules.resolve_origin_path(None, VAULT, HINTS) is None and rules.resolve_origin_path("", VAULT, HINTS) is None
    assert rules.resolve_origin_path("no file named here", VAULT, HINTS) is None


# ------------------------------------------------------------------ parse_hints

def test_an_empty_object_is_no_hints():
    assert parse_hints({}) == NO_HINTS and parse_hints({"version": 1}) == NO_HINTS
    assert NO_HINTS.fingerprint_re is None


@pytest.mark.parametrize("data, message", [
    ([], "must be a JSON object"),
    ({"fingerprint": []}, "unknown key"),
    ({"fingerprints": "x"}, "`fingerprints` must be a list"),
    ({"fingerprints": [""]}, "`fingerprints` must be a list"),
    ({"fingerprints": ["("]}, "not a valid regular expression"),
    ({"personal_notes": [3]}, "`personal_notes` must be a list"),
    ({"whole_titles": "x"}, "`whole_titles` must be a list"),
    ({"note_folders": []}, "`note_folders` must map"),
    ({"note_folders": {"f": "a.md"}}, "`note_folders` must map"),
    ({"note_folders": {"f": [""]}}, "`note_folders` must map"),
])
def test_a_bad_hints_file_is_refused_naming_the_source_and_the_problem(data, message):
    with pytest.raises(ValueError, match=message) as e:
        parse_hints(data, "fact_hints.json")
    assert "fact_hints.json" in str(e.value)


def test_hints_are_immutable_and_order_independent_for_folders():
    a = parse_hints({"note_folders": {"b": ["2.md"], "a": ["1.md"]}})
    b = parse_hints({"note_folders": {"a": ["1.md"], "b": ["2.md"]}})
    assert a == b and a.folder_of("1.md") == "a"
    with pytest.raises(Exception):
        a.fingerprints = ("x",)


# ------------------------------------------------------------------ the loader and the build

def test_the_loader_returns_no_hints_for_an_absent_file_and_the_parsed_ones_otherwise(tmp_path):
    from ingest import shared
    assert shared.load_hints(str(tmp_path)) == NO_HINTS
    (tmp_path / "fact_hints.json").write_text(json.dumps({"fingerprints": ["abc"]}))
    assert shared.load_hints(str(tmp_path)).fingerprints == ("abc",)


@pytest.mark.parametrize("text, message", [("{nope", "not valid JSON"), ('{"fingerprints": 3}', "must be a list")])
def test_the_loader_fails_loudly_on_a_bad_file(tmp_path, text, message):
    from ingest import shared
    (tmp_path / "fact_hints.json").write_text(text)
    with pytest.raises(ValueError, match=message) as e:
        shared.load_hints(str(tmp_path))
    assert "fact_hints.json" in str(e.value)


def test_the_core_source_names_none_of_the_authors_vault():
    """The heuristic's data moved to the private repo; the core keeps only the generic pronoun rule."""
    text = open(rules.__file__, encoding="utf-8").read()
    assert "FINGERPRINT_RE" not in text and "TOP_LEVEL_PERSONAL_FILES" not in text and "HARM_REDUCTION_FILES" not in text
    assert not re.search(r"\bHumira\b|Spondylitis|\d{4}-\d\d-\d\d\|", text, re.IGNORECASE)
