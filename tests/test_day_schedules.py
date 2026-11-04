"""Days with their own schedule: a day (monday ... sunday) with changes uses them instead
of its weekday or weekend schedule. Empty days use their usual one."""
from __future__ import annotations

import pytest
import yaml

from homeassistant.data_entry_flow import FlowResultType

from custom_components.heating_manager.entry_data import schedule_changes, yaml_to_options
from custom_components.heating_manager.schedule_manager import ScheduleManager

from .conftest import FakeTRV, local_dt, make_config, make_room, set_temp
from .test_config_flow import (  # noqa: F401
    configure, edit_zone, entry, env, period, room, shown, start_zone, submit, zone_input, zones,
)

WEEKDAY = [{"start": "06:00", "temperature": 19.0}, {"start": "08:00", "temperature": 17.0},
           {"start": "17:00", "temperature": 19.0}, {"start": "22:00", "temperature": 16.0}]
WEEKEND = [{"start": "08:00", "temperature": 20.0}, {"start": "23:00", "temperature": 16.0}]
HOME_ALL_DAY = [{"start": "07:00", "temperature": 19.5}, {"start": "21:00", "temperature": 17.5}]
LIE_IN = [{"start": "10:00", "temperature": 20.5}]

# 2026-01-12 is a Monday
MON, TUE, WED, THU, FRI, SAT, SUN, NEXT_MON = (12, 13, 14, 15, 16, 17, 18, 19)


def zone(**days):
    return {"name": "Zone", "schedule": {"weekday": WEEKDAY, "weekend": WEEKEND, **days}}


def temp(zone_config, day, hour, minute=0):
    return ScheduleManager(minimum_temp=10.0).get_scheduled_temperature(zone_config, local_dt(2026, 1, day, hour, minute))


# ---------------------------------------------------------------------------
# Which schedule a day uses, and the carry-over between days
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("day", "hour", "minute", "expected"),
    [
        (TUE, 12, 0, 17.0),        # an ordinary weekday
        (WED, 6, 30, 16.0),        # Tuesday's 22:00 carries into Wednesday...
        (WED, 7, 0, 19.5),         # ...until Wednesday's own first change
        (WED, 12, 0, 19.5),        # not the weekday's 08:00 drop
        (WED, 21, 0, 17.5),
        (THU, 5, 59, 17.5),        # Wednesday's last change carries into Thursday
        (THU, 6, 0, 19.0),         # Thursday is a weekday again
        (SAT, 9, 0, 20.0),         # an ordinary weekend day
        (SUN, 9, 0, 16.0),         # Sunday lie-in: Saturday's 23:00 carries on...
        (SUN, 10, 0, 20.5),        # ...until 10:00
        (NEXT_MON, 5, 0, 20.5),    # and Sunday's last change carries into Monday
        (NEXT_MON, 6, 0, 19.0),
    ],
)
def test_days_use_their_own_schedule(day, hour, minute, expected):
    assert temp(zone(wednesday=HOME_ALL_DAY, sunday=LIE_IN), day, hour, minute) == expected


def test_an_empty_day_uses_its_usual_schedule():
    assert temp(zone(wednesday=[]), WED, 12) == 17.0
    assert temp(zone(wednesday=None), WED, 12) == 17.0


def test_upcoming_changes_through_a_day_of_its_own():
    manager = ScheduleManager(minimum_temp=10.0)
    changes = manager.upcoming_changes(zone(wednesday=HOME_ALL_DAY), local_dt(2026, 1, TUE, 23), 3)
    assert changes == [
        (local_dt(2026, 1, WED, 7), 19.5),
        (local_dt(2026, 1, WED, 21), 17.5),
        (local_dt(2026, 1, THU, 6), 19.0),
    ]


def test_with_a_pre_37_schedule():
    """YAML with periods (start, end) for weekdays and weekends, and a day of its own."""
    legacy = {"name": "Zone", "schedule": {
        "weekday": [{"start": "06:00", "end": "22:00", "temperature": 19.0}],
        "weekend": [{"start": "08:00", "end": "23:00", "temperature": 20.0}],
        "friday": [{"start": "06:00", "temperature": 19.0}, {"start": "15:00", "temperature": 21.0}],
    }}
    assert temp(legacy, THU, 16) == 19.0
    assert temp(legacy, FRI, 16) == 21.0
    assert temp(legacy, FRI, 23) == 21.0


# ---------------------------------------------------------------------------
# Stored and imported
# ---------------------------------------------------------------------------

def test_stored_days_are_cleaned_up():
    stored = schedule_changes({
        "weekday": WEEKDAY, "weekend": WEEKEND,
        "wednesday": [{"start": "21:00", "temperature": 17.5}, {"start": "07:00", "temperature": 19.5},
                      {"start": "07:00", "temperature": 30}, {"start": "bad", "temperature": 19}],
        "friday": [],
    }, 10.0)
    assert stored["wednesday"] == HOME_ALL_DAY            # sorted, one per time, invalid dropped
    assert "friday" not in stored                         # empty: not kept


def test_yaml_import_keeps_days():
    options = yaml_to_options(yaml.safe_load("""
zones:
  downstairs:
    schedule:
      weekday:
        - {start: "06:00", temperature: 19}
      wednesday:
        - {start: "07:00", temperature: 19.5}
        - {start: "21:00", temperature: 17.5}
"""))
    assert options["zones"]["downstairs"]["schedule"]["wednesday"] == HOME_ALL_DAY


# ---------------------------------------------------------------------------
# The zone form
# ---------------------------------------------------------------------------

def with_days(data, **days):
    return {**data, "days": {day: [period(*p) for p in changes] for day, changes in days.items()}}


async def test_zone_form_saves_days(hass, entry):
    data = with_days(
        zone_input(weekday=[("06:00", 19)], rooms=[room("Study", sensors=["sensor.study"])]),
        wednesday=[("21:00", 17.5), ("07:00", 19.5)], friday=[],
    )
    result = await submit(hass, await start_zone(hass, entry), data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    schedule = zones(entry)["upstairs"].data["schedule"]
    assert schedule["wednesday"] == HOME_ALL_DAY
    assert "friday" not in schedule


async def test_zone_form_without_the_days_section(hass, entry):
    """Older clients (and automations) that don't send the section."""
    result = await submit(hass, await start_zone(hass, entry), zone_input(weekday=[("06:00", 19)],
                          rooms=[room("Study", sensors=["sensor.study"])]))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(zones(entry)["upstairs"].data["schedule"]) == {"weekday", "weekend"}


async def schedule_page(hass, entry):
    result = await start_zone(hass, entry, "downstairs")
    return await configure(hass, result, shown(result))


def days_section(result):
    return next(value for key, value in result["data_schema"].schema.items() if str(key) == "days")


async def test_days_section_collapsed_unless_used(hass, entry):
    result = await schedule_page(hass, entry)
    assert days_section(result).options["collapsed"] is True
    hass.config_entries.subentries.async_abort(result["flow_id"])

    await edit_zone(hass, entry, days={"wednesday": [period("07:00", 19.5)]})
    result = await schedule_page(hass, entry)
    assert days_section(result).options["collapsed"] is False
    assert shown(result)["days"]["wednesday"] == [{"start": "07:00", "temperature": 19.5}]
    assert shown(result)["days"]["monday"] == []


async def test_reconfigure_clears_a_day(hass, entry):
    await edit_zone(hass, entry, days={"wednesday": [period("07:00", 19.5)]})
    assert "wednesday" in zones(entry)["downstairs"].data["schedule"]
    await edit_zone(hass, entry, days={"wednesday": []})
    assert "wednesday" not in zones(entry)["downstairs"].data["schedule"]


async def test_duplicate_time_in_a_day(hass, entry):
    result = await edit_zone(hass, entry, days={"thursday": [period("07:00", 19.5), period("07:00", 18)]})
    assert result["errors"] == {"base": "duplicate_time"}
    assert result["description_placeholders"]["duplicate"] == "Thursday: 07:00"
    assert "thursday" not in zones(entry)["downstairs"].data["schedule"]


# ---------------------------------------------------------------------------
# In the running system
# ---------------------------------------------------------------------------

async def test_running_system_uses_the_days_schedule(hass, add_trvs, make_coordinator, freezer):
    freezer.move_to(local_dt(2026, 1, WED, 12))
    await add_trvs(FakeTRV("room_trv", current_temperature=18.0))
    set_temp(hass, "sensor.room", 18.0)
    config = make_config({"zone_1": {
        **zone(wednesday=HOME_ALL_DAY), "rooms": {"room": make_room("Room", ["climate.room_trv"], ["sensor.room"])},
    }})
    coordinator = make_coordinator(config)
    await coordinator.async_refresh()
    assert coordinator.data["zone_1"]["rooms"]["room"]["target_temperature"] == 19.5
    assert coordinator.data["zone_1"]["schedule_info"]["next_period"] == {"start": "21:00", "temperature": 17.5}
