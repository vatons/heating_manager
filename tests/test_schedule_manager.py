"""Tests for ScheduleManager: temperature changes, the wrap between days, and converting
pre-3.7 periods (start and end) exactly."""
from __future__ import annotations

from datetime import timedelta
import random

import pytest

from custom_components.heating_manager.schedule_manager import (
    MINUTES_PER_DAY,
    ScheduleManager,
    _in_range,
    _legacy_day,
    convert_legacy_schedule,
    is_legacy_schedule,
    parse_time,
)

from .conftest import local_dt

MONDAY = local_dt(2026, 1, 12)
WEDNESDAY = local_dt(2026, 1, 14)
FRIDAY = local_dt(2026, 1, 16)
SATURDAY = local_dt(2026, 1, 17)
SUNDAY = local_dt(2026, 1, 18)
OUTSIDE = 10.0


def at(base, hour, minute=0):
    return base.replace(hour=hour, minute=minute)


def changes(*pairs):
    return [{"start": start, "temperature": temperature} for start, temperature in pairs]


@pytest.fixture
def sm():
    return ScheduleManager(minimum_temp=OUTSIDE)


# The schedule from the screenshot: each change lasts until the next
USER = {"schedule": {
    "weekday": changes(("06:00", 19.5), ("08:00", 18.5), ("16:00", 19.5), ("21:00", 17.5)),
    "weekend": changes(("08:00", 20.0), ("23:00", 16.0)),
}}


# ---------------------------------------------------------------------------
# Changes within a day
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (6, 0, 19.5),      # a change applies from its time
        (7, 59, 19.5),
        (8, 0, 18.5),
        (15, 59, 18.5),
        (16, 0, 19.5),
        (21, 0, 17.5),
        (23, 59, 17.5),
    ],
)
def test_change_lasts_until_the_next(sm, hour, minute, expected):
    assert sm.get_scheduled_temperature(USER, at(WEDNESDAY, hour, minute)) == expected


def test_last_change_carries_on_overnight(sm):
    """Wednesday's 21:00 → 17.5 lasts until Thursday's 06:00."""
    thursday = WEDNESDAY + timedelta(days=1)
    assert sm.get_scheduled_temperature(USER, at(thursday, 0, 0)) == 17.5
    assert sm.get_scheduled_temperature(USER, at(thursday, 5, 59)) == 17.5
    assert sm.get_scheduled_temperature(USER, at(thursday, 6, 0)) == 19.5


def test_order_doesnt_matter(sm):
    shuffled = {"schedule": {"weekday": list(reversed(USER["schedule"]["weekday"]))}}
    assert sm.get_scheduled_temperature(shuffled, at(WEDNESDAY, 10)) == 18.5


def test_change_at_midnight(sm):
    cfg = {"schedule": {"weekday": changes(("00:00", 16.0), ("07:00", 20.0))}}
    assert sm.get_scheduled_temperature(cfg, at(WEDNESDAY, 0, 0)) == 16.0
    assert sm.get_scheduled_temperature(cfg, at(WEDNESDAY, 23, 59)) == 20.0


def test_single_change_is_all_day(sm):
    cfg = {"schedule": {"weekday": changes(("07:00", 19.0)), "weekend": changes(("07:00", 19.0))}}
    for hour in (0, 6, 7, 23):
        assert sm.get_scheduled_temperature(cfg, at(WEDNESDAY, hour)) == 19.0


# ---------------------------------------------------------------------------
# The wrap between weekdays and weekends
# ---------------------------------------------------------------------------

def test_friday_night_carries_into_saturday(sm):
    """Friday (weekday) 21:00 → 17.5 lasts until Saturday's (weekend) first change at 08:00."""
    assert sm.get_scheduled_temperature(USER, at(FRIDAY, 23, 59)) == 17.5
    assert sm.get_scheduled_temperature(USER, at(SATURDAY, 0, 0)) == 17.5
    assert sm.get_scheduled_temperature(USER, at(SATURDAY, 7, 59)) == 17.5
    assert sm.get_scheduled_temperature(USER, at(SATURDAY, 8, 0)) == 20.0


def test_saturday_night_carries_into_sunday(sm):
    assert sm.get_scheduled_temperature(USER, at(SUNDAY, 3)) == 16.0          # Saturday's 23:00 change
    assert sm.get_scheduled_temperature(USER, at(SUNDAY, 8)) == 20.0


def test_sunday_night_carries_into_monday(sm):
    """Sunday (weekend) 23:00 → 16 lasts until Monday's (weekday) first change at 06:00."""
    assert sm.get_scheduled_temperature(USER, at(SUNDAY, 23, 30)) == 16.0
    assert sm.get_scheduled_temperature(USER, at(MONDAY, 0, 0)) == 16.0
    assert sm.get_scheduled_temperature(USER, at(MONDAY, 5, 59)) == 16.0
    assert sm.get_scheduled_temperature(USER, at(MONDAY, 6, 0)) == 19.5


def test_thursday_night_carries_into_friday_from_the_weekday_schedule(sm):
    assert sm.get_scheduled_temperature(USER, at(FRIDAY, 3)) == 17.5


def test_day_without_changes_uses_outside_temperature(sm):
    """No weekend changes: the weekend is the outside temperature, not Friday's last change."""
    cfg = {"schedule": {"weekday": changes(("06:00", 19.0), ("22:00", 16.0)), "weekend": []}}
    assert sm.get_scheduled_temperature(cfg, at(SATURDAY, 1)) == OUTSIDE
    assert sm.get_scheduled_temperature(cfg, at(SUNDAY, 23)) == OUTSIDE
    # ...and Monday before its first change follows Sunday, which had no changes
    assert sm.get_scheduled_temperature(cfg, at(MONDAY, 3)) == OUTSIDE
    assert sm.get_scheduled_temperature(cfg, at(MONDAY, 7)) == 19.0


@pytest.mark.parametrize("schedule", [None, "oops", {}, {"weekday": "oops"}])
def test_no_or_malformed_schedule_uses_outside_temperature(sm, schedule):
    assert sm.get_scheduled_temperature({"schedule": schedule}, at(WEDNESDAY, 12)) == OUTSIDE


def test_invalid_entries_skipped_and_warned_once(sm, caplog):
    cfg = {"name": "Upstairs", "schedule": {"weekday": [
        {"start": "25:99", "temperature": 30.0},
        {"start": "06:00"},
        "garbage",
        {"start": "06:00", "temperature": 19.0},
    ]}}
    for _ in range(3):
        assert sm.get_scheduled_temperature(cfg, at(WEDNESDAY, 12)) == 19.0
    warnings = [r for r in caplog.records if "invalid time or temperature" in r.getMessage()]
    assert len(warnings) == 3


def test_unquoted_yaml_times(sm):
    import yaml

    cfg = yaml.safe_load("schedule:\n  weekday:\n    - {start: 06:30, temperature: 19.5}\n")
    assert sm.get_scheduled_temperature(cfg, at(WEDNESDAY, 12)) == 19.5


# ---------------------------------------------------------------------------
# Current and next, for the zone entity's attributes
# ---------------------------------------------------------------------------

def test_period_info_during_the_day(sm):
    current, upcoming = sm.get_period_info(USER, at(WEDNESDAY, 7))
    assert current == {"start": "06:00", "end": "08:00", "temperature": 19.5}
    assert upcoming == {"start": "08:00", "temperature": 18.5}


def test_period_info_overnight_into_the_weekend(sm):
    current, upcoming = sm.get_period_info(USER, at(FRIDAY, 22))
    assert current == {"start": "21:00", "end": "08:00", "temperature": 17.5}
    assert upcoming == {"start": "08:00", "temperature": 20.0, "tomorrow": True}

    current, upcoming = sm.get_period_info(USER, at(SATURDAY, 3))
    assert current == {"start": "21:00", "end": "08:00", "temperature": 17.5, "yesterday": True}
    assert upcoming == {"start": "08:00", "temperature": 20.0}


def test_period_info_overnight_into_monday(sm):
    current, upcoming = sm.get_period_info(USER, at(MONDAY, 2))
    assert current == {"start": "23:00", "end": "06:00", "temperature": 16.0, "yesterday": True}
    assert upcoming == {"start": "06:00", "temperature": 19.5}


def test_period_info_skips_changes_to_the_same_temperature(sm):
    cfg = {"schedule": {"weekday": changes(("06:00", 19.0), ("12:00", 19.0), ("18:00", 17.0))}}
    current, upcoming = sm.get_period_info(cfg, at(WEDNESDAY, 7))
    assert current["end"] == "18:00"
    assert upcoming == {"start": "18:00", "temperature": 17.0}


def test_period_info_empty_schedule(sm):
    assert sm.get_period_info({}, at(WEDNESDAY, 12)) == (None, None)


# ---------------------------------------------------------------------------
# Pre-3.7 periods (start and end)
# ---------------------------------------------------------------------------

LEGACY = {"schedule": {
    "weekday": [
        {"start": "00:00", "end": "06:00", "temperature": 16.0},
        {"start": "06:00", "end": "08:00", "temperature": 19.5},
        {"start": "08:00", "end": "16:00", "temperature": 18.0},
        {"start": "16:00", "end": "21:00", "temperature": 20.0},
        {"start": "21:00", "end": "00:00", "temperature": 17.0},
    ],
    "weekend": [
        {"start": "00:00", "end": "08:00", "temperature": 15.0},
        {"start": "08:00", "end": "00:00", "temperature": 21.0},
    ],
}}


def legacy_temperature(schedule: dict, when) -> float:
    """What 3.6 and earlier gave: each day on its own; first matching period, else outside."""
    day = "weekend" if when.weekday() >= 5 else "weekday"
    minute = when.hour * 60 + when.minute
    for start, end, temperature in _legacy_day(schedule.get(day)):
        if _in_range(start, end, minute):
            return temperature
    return OUTSIDE


def assert_same_all_week(sm, schedule):
    """Old and new give the same temperature at every minute of a week, both formats."""
    converted = {"schedule": convert_legacy_schedule(schedule, OUTSIDE)}
    assert not is_legacy_schedule(converted["schedule"])
    for minute in range(7 * MINUTES_PER_DAY):
        when = MONDAY + timedelta(minutes=minute)
        expected = legacy_temperature(schedule, when)
        assert sm.get_scheduled_temperature(converted, when) == expected, when
        assert sm.get_scheduled_temperature({"schedule": schedule}, when) == expected, when


def test_converting_the_old_example(sm):
    converted = convert_legacy_schedule(LEGACY["schedule"], OUTSIDE)
    assert converted == {
        # Weekend nights end at 21.0 and weekday nights at 17.0, so both days keep 00:00
        "weekday": changes(("00:00", 16.0), ("06:00", 19.5), ("08:00", 18.0), ("16:00", 20.0), ("21:00", 17.0)),
        "weekend": changes(("00:00", 15.0), ("08:00", 21.0)),
    }
    assert_same_all_week(sm, LEGACY["schedule"])


def test_converting_gaps(sm):
    """Times outside every period become changes to the outside temperature."""
    schedule = {"weekday": [{"start": "06:30", "end": "22:00", "temperature": 19.0}],
                "weekend": [{"start": "08:00", "end": "23:00", "temperature": 20.0}]}
    assert convert_legacy_schedule(schedule, OUTSIDE) == {
        # Every night ends at the outside temperature, so midnight needs no change
        "weekday": changes(("06:30", 19.0), ("22:00", OUTSIDE)),
        "weekend": changes(("08:00", 20.0), ("23:00", OUTSIDE)),
    }
    assert_same_all_week(sm, schedule)


def test_converting_an_empty_weekend(sm):
    schedule = {"weekday": [{"start": "06:00", "end": "22:00", "temperature": 19.0}], "weekend": []}
    assert convert_legacy_schedule(schedule, OUTSIDE)["weekend"] == []
    assert_same_all_week(sm, schedule)


def test_converting_periods_across_midnight(sm):
    """Before 3.7 a weekend period 22:00-02:00 also covered Saturday 00:00-02:00 (after Friday)."""
    schedule = {"weekday": [{"start": "07:00", "end": "23:00", "temperature": 19.0}],
                "weekend": [{"start": "22:00", "end": "02:00", "temperature": 21.0}]}
    assert_same_all_week(sm, schedule)


def test_converting_overlaps_keeps_the_first_listed(sm):
    schedule = {"weekday": [{"start": "06:00", "end": "12:00", "temperature": 20.0},
                            {"start": "08:00", "end": "10:00", "temperature": 22.0}]}
    assert_same_all_week(sm, schedule)


def test_converting_all_day_and_end_of_day(sm):
    for schedule in (
        {"weekday": [{"start": "00:00", "end": "00:00", "temperature": 19.0}],
         "weekend": [{"start": "00:00", "end": "00:00", "temperature": 19.0}]},
        {"weekday": [{"start": "18:00", "end": "24:00", "temperature": 20.0}]},
        {"weekday": [{"start": "00:00", "end": "23:59", "temperature": 19.0}]},
    ):
        assert_same_all_week(sm, schedule)


def test_converting_random_schedules(sm):
    """Many random old schedules: identical every minute of the week after converting."""
    rng = random.Random(37)
    times = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
    for _ in range(40):
        schedule = {
            day: [
                {"start": rng.choice(times), "end": rng.choice(times + ["24:00"]),
                 "temperature": rng.choice([15.0, 17.5, 19.0, 21.0, OUTSIDE])}
                for _ in range(rng.randint(0, 4))
            ]
            for day in ("weekday", "weekend")
        }
        assert_same_all_week(sm, schedule)


@pytest.mark.parametrize("first_day", [SATURDAY, SUNDAY, MONDAY, FRIDAY])
def test_first_lookup_of_an_old_schedule_on_any_day(first_day):
    """Converting happens on the first lookup; it mustn't depend on which day that is."""
    sm = ScheduleManager(minimum_temp=OUTSIDE)
    when = at(first_day, 9)
    assert sm.get_scheduled_temperature(LEGACY, when) == legacy_temperature(LEGACY["schedule"], when)


def test_is_legacy_schedule():
    assert is_legacy_schedule(LEGACY["schedule"])
    assert not is_legacy_schedule(USER["schedule"])
    assert not is_legacy_schedule({})
    assert not is_legacy_schedule(None)


@pytest.mark.parametrize(
    ("value", "minutes"),
    [
        ("06:30", 390), ("6:30", 390), ("00:00", 0), ("23:59", 1439), ("24:00", 1440),
        ("21:00:00", 1260), (" 07:15 ", 435), (1260, 1260), (0, 0),
        ("25:00", None), ("12:60", None), ("noon", None), ("", None), (None, None),
        (-5, None), (1441, None), (True, None), (19.5, None),
    ],
)
def test_parse_time(value, minutes):
    assert parse_time(value) == minutes
