"""Temperature changes in a running system: the wrap between weekdays and weekends, and
upgrading schedules made of periods (start and end) from the original YAML, 2.x and 3.6
without changing the temperature at any time of the week."""
from __future__ import annotations

from datetime import timedelta

import pytest
import yaml

from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heating_manager.const import DOMAIN
from custom_components.heating_manager.entry_data import default_settings, entry_to_runtime, yaml_to_options
from custom_components.heating_manager.schedule_manager import MINUTES_PER_DAY, ScheduleManager, _in_range, _legacy_day

from .conftest import FakeTRV, local_dt, make_config, make_room, set_temp
from .test_config_flow import env, zones  # noqa: F401

MONDAY = local_dt(2026, 1, 12)      # for ScheduleManager directly (no time zone conversion)

# Different weekday and weekend schedules, as in the screenshot
SCHEDULE = {
    "weekday": [
        {"start": "06:00", "temperature": 19.5}, {"start": "08:00", "temperature": 18.5},
        {"start": "16:00", "temperature": 19.5}, {"start": "21:00", "temperature": 17.5},
    ],
    "weekend": [{"start": "08:30", "temperature": 20.0}, {"start": "23:00", "temperature": 16.0}],
}


@pytest.mark.parametrize(
    ("day", "hour", "minute", "expected"),
    [
        (16, 20, 59, 19.5),        # Friday
        (16, 21, 0, 17.5),
        (17, 0, 0, 17.5),          # Saturday: Friday's 21:00 change carries on...
        (17, 8, 29, 17.5),
        (17, 8, 30, 20.0),         # ...until Saturday's first change
        (18, 2, 0, 16.0),          # Sunday: Saturday's 23:00 change
        (18, 23, 30, 16.0),
        (19, 0, 0, 16.0),          # Monday: Sunday's 23:00 change carries on...
        (19, 5, 59, 16.0),
        (19, 6, 0, 19.5),          # ...until Monday's first change
    ],
)
async def test_weekday_weekend_wrap_in_the_running_system(
    hass, add_trvs, make_coordinator, freezer, day, hour, minute, expected
):
    # Built here, in Home Assistant's time zone (set up for the test)
    freezer.move_to(local_dt(2026, 1, day, hour, minute))
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, "sensor.room", 17.0)
    coordinator = make_coordinator(make_config({"zone_1": {
        "name": "Zone 1", "schedule": SCHEDULE, "rooms": {"room": make_room("Room", ["climate.room_trv"], ["sensor.room"])},
    }}))
    await coordinator.async_refresh()
    assert coordinator.data["zone_1"]["rooms"]["room"]["target_temperature"] == expected


# ---------------------------------------------------------------------------
# Upgrading periods (start, end): the same temperature at every minute of the week
# ---------------------------------------------------------------------------

def old_temperature(schedule: dict, outside: float, when) -> float:
    """What versions before 3.7 gave: each day on its own; first matching period, else outside."""
    day = "weekend" if when.weekday() >= 5 else "weekday"
    minute = when.hour * 60 + when.minute
    for start, end, temperature in _legacy_day((schedule or {}).get(day)):
        if _in_range(start, end, minute):
            return temperature
    return outside


def assert_same_all_week(old_schedule: dict, new_schedule: dict, outside: float):
    assert all("end" not in c for day in new_schedule.values() for c in day), "not converted"
    manager = ScheduleManager(minimum_temp=outside)
    zone = {"schedule": new_schedule}
    for minute in range(7 * MINUTES_PER_DAY):
        when = MONDAY + timedelta(minutes=minute)
        assert manager.get_scheduled_temperature(zone, when) == old_temperature(old_schedule, outside, when), when


# heating_manager.yaml as used before the UI existed: periods with an end, unquoted times,
# "24:00", a gap at night and different weekend times
ORIGINAL_YAML = """
minimum_temp: 16
zones:
  downstairs:
    name: Downstairs
    schedule:
      weekday:
        - {start: 06:00, end: 08:30, temperature: 20}
        - {start: "17:00", end: "22:30", temperature: 20.5}
      weekend:
        - {start: "08:00", end: "24:00", temperature: 20}
    rooms:
      lounge:
        trvs: [climate.lounge_trv]
        sensors: [sensor.lounge]
  upstairs:
    name: Upstairs
    schedule:
      weekday:
        - {start: "21:00", end: "07:00", temperature: 18}
      weekend:
        - {start: "22:00", end: "09:00", temperature: 18.5}
    rooms:
      bedroom:
        trvs: [climate.study_trv]
        sensors: [sensor.study]
"""


async def test_original_yaml_imports_with_the_same_temperatures(hass, env, tmp_path):
    """configuration.yaml's heating_manager: imports on first start, as before."""
    path = tmp_path / "heating_manager.yaml"
    path.write_text(ORIGINAL_YAML)
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: {"config_file": str(path)}})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.version == 4
    original = yaml.safe_load(ORIGINAL_YAML)
    for zone_id in ("downstairs", "upstairs"):
        # Compared with how the file's periods (as cleaned up on import) used to run
        old = yaml_to_options(original)["zones"][zone_id]["schedule"]
        assert_same_all_week(old, zones(entry)[zone_id].data["schedule"], 16.0)
    # And the running system uses them
    config, _ = entry_to_runtime(entry)
    # Weekday nights (21:00-07:00) and weekend nights (22:00-09:00) end at different
    # temperatures, so midnight keeps an explicit change
    assert config["zones"]["upstairs"]["schedule"]["weekday"] == [
        {"start": "00:00", "temperature": 18.0}, {"start": "07:00", "temperature": 16.0},
        {"start": "21:00", "temperature": 18.0},
    ]


async def test_configure_import_from_yaml_file(hass, env):
    """Configure → Import from YAML file converts the same way."""
    from .test_config_flow import entry_from_options, form, menu, open_options, setup_entry

    entry = await setup_entry(hass, entry_from_options({"settings": {}, "zones": {}}))
    path = hass.config.path("hm_upgrade_test.yaml")
    with open(path, "w") as fh:
        fh.write(ORIGINAL_YAML)
    try:
        flow_id = await open_options(hass, entry)
        await menu(hass, flow_id, "import_yaml")
        await form(hass, flow_id, {"path": "hm_upgrade_test.yaml", "import_settings": True})
        await form(hass, flow_id, {"confirm": True})
        await hass.async_block_till_done()
    finally:
        import os
        os.remove(path)
    old = yaml_to_options(yaml.safe_load(ORIGINAL_YAML))["zones"]
    # The file's own temperature outside schedule (16) is what the converted gaps use
    assert entry.options["settings"]["minimum_temp"] == 16
    for zone_id in ("downstairs", "upstairs"):
        assert_same_all_week(old[zone_id]["schedule"], zones(entry)[zone_id].data["schedule"], 16.0)


async def test_upgrade_from_2x(hass, env):
    """2.x kept zones in the entry's options, with periods."""
    options = yaml_to_options(yaml.safe_load(ORIGINAL_YAML))
    options["settings"]["minimum_temp"] = 15.0
    entry = MockConfigEntry(domain=DOMAIN, title="Heating Manager", data={}, options=options, version=1)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 4
    for zone_id in ("downstairs", "upstairs"):
        assert_same_all_week(options["zones"][zone_id]["schedule"], zones(entry)[zone_id].data["schedule"], 15.0)


@pytest.mark.parametrize("same_as_weekdays", [False, True])
async def test_upgrade_from_36(hass, env, same_as_weekdays):
    """3.2-3.6 stored each zone's periods in its subentry."""
    weekday = [{"start": "06:30", "end": "08:00", "temperature": 19.5},
               {"start": "16:00", "end": "21:00", "temperature": 20.0}]
    weekend = weekday if same_as_weekdays else [{"start": "08:00", "end": "00:00", "temperature": 21.0}]
    settings = {**default_settings(), "minimum_temp": 14.0}
    entry = MockConfigEntry(
        domain=DOMAIN, title="Heating Manager", data={}, version=3, options={"settings": settings},
        subentries_data=[{
            "subentry_type": "zone", "title": "Downstairs · 0 rooms", "unique_id": "downstairs",
            "data": {"zone_id": "downstairs", "name": "Downstairs", "rooms": [],
                     "weekend_same_as_weekday": same_as_weekdays,
                     # 3.x stored the weekday copy when "same as weekdays"; an older empty one is fine too
                     "schedule": {"weekday": weekday, "weekend": weekend if not same_as_weekdays else []}},
        }],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 4
    data = zones(entry)["downstairs"].data
    assert data["weekend_same_as_weekday"] is same_as_weekdays
    assert_same_all_week({"weekday": weekday, "weekend": weekend}, data["schedule"], 14.0)


def test_new_style_yaml():
    """A YAML file can also give temperature changes (no end)."""
    options = yaml_to_options(yaml.safe_load("""
zones:
  z:
    schedule:
      weekday:
        - {start: "21:00", temperature: 17.5}
        - {start: 06:00, temperature: 19.5}
"""))
    assert options["zones"]["z"]["schedule"]["weekday"] == [
        {"start": "06:00", "temperature": 19.5}, {"start": "21:00", "temperature": 17.5},
    ]
