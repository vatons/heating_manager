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


@pytest.mark.xfail(reason="BUG: start == end matches nothing; 00:00-00:00 should mean all day")
def test_00_00_to_00_00_means_all_day(sm):
    """A period from midnight to midnight should cover the whole day."""
    cfg = {"schedule": {"weekday": [{"start": "00:00", "end": "00:00", "temperature": 19.0}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 19.0


def test_00_00_to_23_59_leaves_final_minute_uncovered(sm):
    """Documents current behaviour: "23:59" end is exclusive, so 23:59 falls to minimum."""
    cfg = {"schedule": {"weekday": [{"start": "00:00", "end": "23:59", "temperature": 19.0}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 23, 58)) == 19.0
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 23, 59)) == 10.0


@pytest.mark.xfail(reason="BUG: times are compared as strings, so '6:30' sorts after '21:00'")
def test_unpadded_times_are_compared_correctly(sm):
    """YAML like `start: "6:30"` is easy to write; it must not break string comparison."""
    cfg = {"schedule": {"weekday": [{"start": "6:30", "end": "21:00", "temperature": 19.5}]}}
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 12)) == 19.5
    # String comparison treats this as a midnight-spanning period, heating at 05:00.
    assert sm.get_scheduled_temperature(cfg, at(WEEKDAY, 5)) == 10.0


@pytest.mark.xfail(
    raises=TypeError,
    reason="BUG: unquoted YAML times become ints (21:00 -> 1260) and crash the update",
)
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
