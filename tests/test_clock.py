"""clock: the system and fixed clocks, and picking one from the environment. Nothing is ambient: a Clock is an
object that is handed to the code that needs the time."""
import re
from datetime import datetime, timedelta, timezone

import pytest

import clock

ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


def test_the_system_clock_is_utc_with_an_explicit_offset_and_whole_seconds():
    c = clock.SystemClock()
    assert ISO_UTC.match(c.now_iso())
    assert c.now().utcoffset() == timedelta(0)


def test_a_fixed_clock_always_answers_the_same_instant():
    c = clock.FixedClock("2026-10-03T08:00:00+00:00")
    assert c.now_iso() == "2026-10-03T08:00:00+00:00" and c.now_iso() == c.now_iso()
    assert c.now() == datetime(2026, 10, 3, 8, tzinfo=timezone.utc)


def test_two_fixed_clocks_are_independent():
    a, b = clock.FixedClock("2026-10-03T08:00:00+00:00"), clock.FixedClock("2027-01-01T00:00:00+00:00")
    assert a.now_iso() == "2026-10-03T08:00:00+00:00" and b.now_iso() == "2027-01-01T00:00:00+00:00"
    assert not clock.SystemClock().now_iso().startswith("2026-10-03T08:00:00")


def test_a_fixed_clock_normalises_other_offsets_to_utc():
    assert clock.FixedClock("2026-10-03T08:00:00-05:00").now_iso() == "2026-10-03T13:00:00+00:00"


def test_naive_times_are_rejected():
    with pytest.raises(ValueError, match="no UTC offset"):
        clock.FixedClock("2026-10-03T08:00:00")
    with pytest.raises(ValueError, match="no UTC offset"):
        clock.FixedClock(datetime(2026, 10, 3))


def test_a_fixed_clock_accepts_an_aware_datetime():
    assert clock.FixedClock(datetime(2026, 10, 3, 8, tzinfo=timezone.utc)).now_iso() == "2026-10-03T08:00:00+00:00"


def test_the_environment_picks_a_fixed_clock_for_subprocesses():
    c = clock.from_environment({clock.ENV_VAR: "2026-10-03T08:00:00+00:00"})
    assert isinstance(c, clock.FixedClock) and c.now_iso() == "2026-10-03T08:00:00+00:00"
    assert type(clock.from_environment({})) is clock.SystemClock
    assert type(clock.from_environment({clock.ENV_VAR: ""})) is clock.SystemClock


def test_a_bad_environment_value_is_an_error_not_a_silent_real_clock():
    with pytest.raises(ValueError):
        clock.from_environment({clock.ENV_VAR: "garbage"})


def test_the_container_uses_the_clock_it_is_given():
    from locations import Locations
    import container
    loc = Locations(knowledge_db_dir="/x/db", private_data_dir="/x/data", notes_vault="/x/v", health_dir="/x/h")
    fixed = clock.FixedClock("2026-10-03T08:00:00+00:00")
    app = container.Container(loc, fixed)
    assert app.clock is fixed
    assert app.add_fact.deps.clock is fixed and app.review.deps.clock is fixed and app.queries.deps.clock is fixed


def test_two_containers_keep_locations_and_clocks_independent():
    from locations import Locations
    import container
    first_locations = Locations(knowledge_db_dir="/one/db", private_data_dir="/one/data",
                                notes_vault="/one/vault", health_dir="/one/health")
    second_locations = Locations(knowledge_db_dir="/two/db", private_data_dir="/two/data",
                                 notes_vault="/two/vault", health_dir="/two/health")
    first_clock = clock.FixedClock("2026-10-03T08:00:00+00:00")
    second_clock = clock.FixedClock("2027-02-04T09:30:00+00:00")
    first = container.Container(first_locations, first_clock)
    second = container.Container(second_locations, second_clock)
    assert first.locations is first_locations and second.locations is second_locations
    assert first.clock is first_clock and second.clock is second_clock
    assert first.add_fact.deps.locations is first_locations and second.add_fact.deps.locations is second_locations
    assert first.add_fact.deps.clock is first_clock and second.add_fact.deps.clock is second_clock
    assert first.add_fact is first.add_fact and first.add_fact is not second.add_fact
