import re
from datetime import datetime, timedelta, timezone

import pytest

import clock

ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


def test_now_iso_is_utc_with_explicit_offset_and_whole_seconds():
    assert ISO_UTC.match(clock.now_iso())


def test_now_is_timezone_aware_utc():
    assert clock.now().utcoffset() == timedelta(0)


def test_frozen_pins_the_time_and_restores_it():
    with clock.frozen("2026-10-03T08:00:00+00:00"):
        assert clock.now_iso() == "2026-10-03T08:00:00+00:00"
        with clock.frozen("2027-01-01T00:00:00+00:00"):
            assert clock.now_iso() == "2027-01-01T00:00:00+00:00"
        assert clock.now_iso() == "2026-10-03T08:00:00+00:00"
    assert clock.now_iso() != "2026-10-03T08:00:00+00:00"


def test_frozen_restores_after_an_exception():
    with pytest.raises(RuntimeError):
        with clock.frozen("2026-10-03T08:00:00+00:00"):
            raise RuntimeError
    assert not clock.now_iso().startswith("2026-10-03T08:00:00")


def test_frozen_normalises_other_offsets_to_utc():
    with clock.frozen("2026-10-03T08:00:00-05:00"):
        assert clock.now_iso() == "2026-10-03T13:00:00+00:00"


def test_naive_times_are_rejected():
    with pytest.raises(ValueError):
        with clock.frozen("2026-10-03T08:00:00"):
            pass
    with pytest.raises(ValueError):
        with clock.frozen(datetime(2026, 10, 3)):
            pass


def test_frozen_accepts_an_aware_datetime():
    with clock.frozen(datetime(2026, 10, 3, 8, tzinfo=timezone.utc)):
        assert clock.now_iso() == "2026-10-03T08:00:00+00:00"


def test_env_var_freezes_for_subprocesses(monkeypatch):
    monkeypatch.setenv(clock.ENV_VAR, "2026-10-03T08:00:00+00:00")
    assert clock.now_iso() == "2026-10-03T08:00:00+00:00"


def test_bad_env_var_is_an_error_not_a_silent_real_clock(monkeypatch):
    monkeypatch.setenv(clock.ENV_VAR, "garbage")
    with pytest.raises(ValueError):
        clock.now_iso()
