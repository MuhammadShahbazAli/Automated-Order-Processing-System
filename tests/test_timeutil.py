"""Schedule arithmetic. No database needed."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from app.timeutil import compute_next_run, get_zone

KARACHI = ZoneInfo("Asia/Karachi")      # UTC+5, no daylight saving
UTC = timezone.utc


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def test_next_run_later_today():
    assert compute_next_run(utc(2026, 10, 12, 2, 0), time(8, 0), KARACHI) == utc(2026, 10, 12, 3, 0)


def test_exactly_at_run_time_means_tomorrow():
    assert compute_next_run(utc(2026, 10, 12, 3, 0), time(8, 0), KARACHI) == utc(2026, 10, 13, 3, 0)


def test_after_run_time_means_tomorrow():
    assert compute_next_run(utc(2026, 10, 12, 4, 0), time(8, 0), KARACHI) == utc(2026, 10, 13, 3, 0)


def test_local_midnight_boundary():
    # 23:30 UTC is 04:30 the next day in Karachi, so 03:00 local is still ahead of us on that local day
    assert compute_next_run(utc(2026, 10, 12, 23, 30), time(3, 0), KARACHI) == utc(2026, 10, 13, 22, 0)


def test_result_is_in_utc_and_on_the_minute():
    result = compute_next_run(utc(2026, 10, 12, 9, 17, 41), time(9, 30), KARACHI)
    assert result.tzinfo == UTC and (result.second, result.microsecond) == (0, 0)


def test_follows_daylight_saving_changes():
    new_york = ZoneInfo("America/New_York")           # clocks jump forward on 8 March 2026
    assert compute_next_run(utc(2026, 3, 7, 20, 0), time(8, 0), new_york) == utc(2026, 3, 8, 12, 0)
    assert compute_next_run(utc(2026, 3, 8, 13, 0), time(8, 0), new_york) == utc(2026, 3, 9, 12, 0)


def test_unknown_time_zone_gives_a_helpful_error():
    with pytest.raises(RuntimeError, match="tzdata"):
        get_zone("Mars/Olympus_Mons")
