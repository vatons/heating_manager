"""Tests for ScheduleManager."""
import pytest

from custom_components.heating_manager.schedule_manager import ScheduleManager

from .conftest import local_dt

WEEKDAY = local_dt(2026, 1, 14)   # Wednesday
SATURDAY = local_dt(2026, 1, 17)
SUNDAY = local_dt(2026, 1, 18)

USER_SCHEDULE = {
    "schedule": {
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
    }
}


def at(base, hour, minute=0):
    return base.replace(hour=hour, minute=minute)


@pytest.fixture
def sm():
    return ScheduleManager(minimum_temp=10.0)


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, 16.0),
        (5, 59, 16.0),
        (6, 0, 19.5),     # start is inclusive
        (7, 59, 19.5),
        (8, 0, 18.0),     # end is exclusive
        (15, 59, 18.0),
        (16, 0, 20.0),
        (20, 59, 20.0),
        (21, 0, 17.0),
        (23, 59, 17.0),   # "21:00"-"00:00" period runs until midnight
    ],
)
def test_weekday_periods_cover_whole_day(sm, hour, minute, expected):
    assert sm.get_scheduled_temperature(USER_SCHEDULE, at(WEEKDAY, hour, minute)) == expected


@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_weekend_schedule_used_on_weekend(sm, day):
    assert sm.get_scheduled_temperature(USER_SCHEDULE, at(day, 7)) == 15.0
    assert sm.get_scheduled_temperature(USER_SCHEDULE, at(day, 23, 30)) == 21.0


def test_gap_in_schedule_uses_minimum_temp(sm):
    cfg = {"schedule": {"weekday": [{"start": "06:30", "end": "21:00", "temperature": 19.5}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 6, 29)) == 10.0
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 6, 30)) == 19.5
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 21, 0)) == 10.0


def test_no_schedule_uses_minimum_temp(sm):
    assert sm.get_scheduled_temperature({}, at(WEEKDAY, 12)) == 10.0
    assert sm.get_scheduled_temperature({"schedule": {}}, at(SATURDAY, 12)) == 10.0


def test_missing_weekend_schedule_uses_minimum_temp(sm):
    cfg = {"schedule": {"weekday": [{"start": "00:00", "end": "23:59", "temperature": 19.0}]}}
    assert sm.get_scheduled_temperature(cfg, at(SATURDAY, 12)) == 10.0


def test_period_without_temperature_uses_minimum(sm):
    cfg = {"schedule": {"weekday": [{"start": "00:00", "end": "23:59"}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 10.0


def test_midnight_spanning_period(sm):
    cfg = {"schedule": {"weekday": [{"start": "22:00", "end": "06:00", "temperature": 15.0}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 23)) == 15.0
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 3)) == 15.0
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 6)) == 10.0
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 10.0


def test_00_00_to_00_00_means_all_day(sm):
    """A period from midnight to midnight should cover the whole day."""
    cfg = {"schedule": {"weekday": [{"start": "00:00", "end": "00:00", "temperature": 19.0}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 19.0


def test_00_00_to_23_59_leaves_final_minute_uncovered(sm):
    """Documents current behaviour: "23:59" end is exclusive, so 23:59 falls to minimum."""
    cfg = {"schedule": {"weekday": [{"start": "00:00", "end": "23:59", "temperature": 19.0}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 23, 58)) == 19.0
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 23, 59)) == 10.0


def test_unpadded_times_are_compared_correctly(sm):
    """YAML like `start: "6:30"` is easy to write; it must not break string comparison."""
    cfg = {"schedule": {"weekday": [{"start": "6:30", "end": "21:00", "temperature": 19.5}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 19.5
    # String comparison treats this as a midnight-spanning period, heating at 05:00.
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 5)) == 10.0


def test_unquoted_yaml_times_are_supported(sm):
    """Unquoted `start: 06:30` is parsed by YAML 1.1 as the integer 390 (sexagesimal)."""
    import yaml

    cfg = yaml.safe_load(
        "schedule:\n  weekday:\n    - {start: 06:30, end: 21:00, temperature: 19.5}\n"
    )
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 19.5


def test_first_matching_period_wins_for_overlaps(sm):
    cfg = {
        "schedule": {
            "weekday": [
                {"start": "06:00", "end": "12:00", "temperature": 20.0},
                {"start": "08:00", "end": "10:00", "temperature": 22.0},
            ]
        }
    }
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 9)) == 20.0


@pytest.mark.parametrize(
    ("start", "end", "current", "expected"),
    [
        (None, "10:00", "09:00", False),
        ("08:00", None, "09:00", False),
        ("08:00", "10:00", "10:00", False),
        ("08:00", "10:00", "08:00", True),
        ("22:00", "06:00", "22:00", True),
        ("22:00", "06:00", "05:59", True),
    ],
)
def test_is_time_in_period(sm, start, end, current, expected):
    assert sm.is_time_in_period(start, end, current) is expected


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
    from custom_components.heating_manager.schedule_manager import parse_time

    assert parse_time(value) == minutes


def test_end_of_day_24_00(sm):
    cfg = {"schedule": {"weekday": [{"start": "18:00", "end": "24:00", "temperature": 20.0}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 23, 59)) == 20.0
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 17, 59)) == 10.0


def test_invalid_periods_skipped_and_warned_once(sm, caplog):
    cfg = {
        "name": "Upstairs",
        "schedule": {
            "weekday": [
                {"start": "07:00", "end": "25:99", "temperature": 30.0},
                {"start": "06:00", "temperature": 30.0},
                "garbage",
                {"start": "06:00", "end": "22:00", "temperature": 19.0},
            ]
        },
    }
    for _ in range(3):
        assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 19.0
    warnings = [r for r in caplog.records if "invalid start/end time" in r.getMessage()]
    assert len(warnings) == 3


@pytest.mark.parametrize("schedule", [None, "oops", {"weekday": "oops"}])
def test_malformed_schedule_uses_minimum(sm, schedule):
    assert sm.get_scheduled_temperature({"schedule": schedule}, at(WEEKDAY, 12)) == 10.0


def test_period_info_current_and_next(sm):
    current, nxt = sm.get_period_info(USER_SCHEDULE, at(WEEKDAY, 7))
    assert current == {"start": "06:00", "end": "08:00", "temperature": 19.5}
    assert nxt == {"start": "08:00", "end": "16:00", "temperature": 18.0}


def test_period_info_next_is_earliest_upcoming_even_if_unsorted(sm):
    cfg = {"schedule": {"weekday": [
        {"start": "18:00", "end": "22:00", "temperature": 20.0},
        {"start": "13:00", "end": "15:00", "temperature": 19.0},
    ]}}
    current, nxt = sm.get_period_info(cfg, at(WEEKDAY, 12))
    assert current is None
    assert nxt["start"] == "13:00"


def test_period_info_rolls_over_to_tomorrow(sm):
    friday = local_dt(2026, 1, 16, 22)
    current, nxt = sm.get_period_info(USER_SCHEDULE, friday)
    assert current["start"] == "21:00"
    assert nxt == {"start": "00:00", "end": "08:00", "temperature": 15.0, "tomorrow": True}


def test_period_info_normalises_unquoted_yaml_times(sm):
    import yaml

    cfg = yaml.safe_load(
        "schedule:\n  weekday:\n    - {start: 06:30, end: 21:00, temperature: 19.5}\n"
    )
    current, _ = sm.get_period_info(cfg, at(WEEKDAY, 12))
    assert current == {"start": "06:30", "end": "21:00", "temperature": 19.5}


def test_period_info_empty_schedule(sm):
    assert sm.get_period_info({}, at(WEEKDAY, 12)) == (None, None)
