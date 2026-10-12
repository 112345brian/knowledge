"""FactState: the lifecycle rules as types with behaviour. The wording is what the CLI and the inbox show, so
it is pinned here; test_lifecycle.py pins that the decisions built on it behave as before."""
import fact_state
from fact_state import FactState, Status, Visibility, supersession_cycle


def test_status_and_visibility_compare_equal_to_the_plain_strings_in_the_data_files():
    assert Status.ACTIVE == "active" and "pending" == Status.PENDING
    assert Visibility.PRIVATE == "private" and {"status": "retracted"}["status"] == Status.RETRACTED


def test_review_word_names_the_outcome_and_passes_unknown_values_through():
    assert fact_state.review_word("active") == "approved"
    assert fact_state.review_word("retracted") == "rejected"
    assert fact_state.review_word("pending") == "pending"
    assert fact_state.review_word("superseded") == "superseded"
    assert fact_state.review_word("bogus") == "bogus" and fact_state.review_word(None) is None


def test_a_pending_or_retracted_fact_cannot_be_superseded_and_says_why():
    assert "pending; approve or reject" in FactState("pending").why_not_supersedable()
    assert "retracted; reverse that" in FactState("retracted").why_not_supersedable()
    assert FactState("active").why_not_supersedable() is None
    assert FactState("superseded", superseded_by="k").why_not_supersedable() is None


def test_a_replacement_must_be_neither_retracted_nor_pending():
    assert FactState("retracted").why_not_a_replacement("r") == "the replacement 'r' is retracted"
    assert FactState("pending").why_not_a_replacement("r") == "the replacement 'r' is pending; approve it first"
    assert FactState("active").why_not_a_replacement("r") is None


def test_visibility_change_is_unchanged_lower_or_raise():
    assert FactState("active", "private").visibility_change_to("private") == "unchanged"
    assert FactState("active", "private").visibility_change_to("normal") == "lower"
    assert FactState("active", "normal").visibility_change_to("private") == "raise"


def test_of_reads_a_state_row_and_tolerates_missing_optional_fields():
    s = FactState.of({"status": "superseded", "superseded_by": "b", "visibility": "normal", "statement": "S."})
    assert s.is_superseded and s.is_superseded_by("b") and not s.is_superseded_by("c") and s.statement == "S."
    bare = FactState.of({"status": "active"})
    assert bare.visibility == "private" and bare.superseded_by is None


def test_supersession_cycle_follows_the_replacement_chain_and_stops_on_loops():
    states = {"a": {"superseded_by": "b"}, "b": {"superseded_by": "c"}, "c": {"superseded_by": None}}
    assert supersession_cycle(states, "c", "a") is True       # a -> b -> c: superseding c by a closes the loop
    assert supersession_cycle(states, "a", "c") is False
    assert supersession_cycle(states, "x", "unknown") is False  # a replacement the log does not know
    looped = {"p": {"superseded_by": "q"}, "q": {"superseded_by": "p"}}
    assert supersession_cycle(looped, "z", "p") is False       # an existing loop elsewhere must terminate
