"""Skip to next: a room or zone goes to its next scheduled temperature now. It's a manual
temperature equal to the next scheduled one, so when that change comes the schedule
carries on at the same temperature."""
from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.heating_manager.const import DOMAIN
from custom_components.heating_manager.schedule_manager import ScheduleManager

from .conftest import FakeTRV, entry_from_options, local_dt, set_temp
from .test_boost_entities import settle

ROOM = "climate.downstairs_lounge"
STUDY = "climate.downstairs_study"
ZONE = "climate.downstairs"
GLOBAL = "climate.heating_manager"
ROOM_SKIP = "button.downstairs_lounge_skip_to_next"
ZONE_SKIP = "button.downstairs_skip_to_next"

# 19 °C from 06:00, 17 °C from 21:00, every day
SCHEDULE = {day: [{"start": "06:00", "temperature": 19.0}, {"start": "21:00", "temperature": 17.0}]
            for day in ("weekday", "weekend")}


def options(schedule=SCHEDULE):
    return {
        "settings": {},
        "zones": {
            "downstairs": {
                "name": "Downstairs",
                "schedule": schedule,
                "rooms": {
                    "lounge": {"name": "Lounge", "trvs": ["climate.lounge_trv"],
                               "sensors": [{"temperature": "sensor.lounge"}], "temperature_offset": -1.0},
                    "study": {"name": "Study", "trvs": ["climate.study_trv"], "sensors": [{"temperature": "sensor.study"}]},
                },
            },
            "upstairs": {
                "name": "Upstairs",
                # never changes: nothing to skip to
                "schedule": {day: [{"start": "00:00", "temperature": 19.0}] for day in ("weekday", "weekend")},
                "rooms": {"bedroom": {"name": "Bedroom", "trvs": ["climate.bedroom_trv"],
                                      "sensors": [{"temperature": "sensor.bedroom"}]}},
            },
        },
    }


@pytest.fixture
async def setup(hass: HomeAssistant, add_trvs, freezer):
    entries = []

    async def _setup(hour=12, schedule=SCHEDULE):
        freezer.move_to(local_dt(hour=hour))
        await add_trvs(*(FakeTRV(f"{name}_trv", current_temperature=18.0) for name in ("lounge", "study", "bedroom")))
        for name in ("lounge", "study", "bedroom"):
            set_temp(hass, f"sensor.{name}", 18.0)
        entry = entry_from_options(options(schedule))
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry

    yield _setup
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)


def target(hass, entity_id):
    return hass.states.get(entity_id).attributes["temperature"]


def attrs(hass, entity_id):
    return hass.states.get(entity_id).attributes


async def press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await settle(hass)


async def service(hass, entity_id, name="skip_to_next"):
    await hass.services.async_call(DOMAIN, name, {"entity_id": entity_id}, blocking=True)
    await settle(hass)


async def at(hass, entry, freezer, hour, day=14):
    freezer.move_to(local_dt(day=day, hour=hour))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

async def test_room_skips_to_its_next_temperature_with_its_offset(hass, setup):
    await setup()
    assert target(hass, ROOM) == 18.0                     # 19 - 1
    await press(hass, ROOM_SKIP)
    assert target(hass, ROOM) == 16.0                     # 17 - 1, not 17
    assert attrs(hass, ROOM)["manual_override"] == {"active": True, "temperature": 16.0, "skipped": True}
    assert attrs(hass, ROOM)["preset_mode"] == "manual"
    assert target(hass, STUDY) == 19.0                    # other rooms unaffected


async def test_schedule_carries_on_at_the_skipped_to_change(hass, setup, freezer):
    entry = await setup()
    await press(hass, ROOM_SKIP)
    await at(hass, entry, freezer, 20)
    assert target(hass, ROOM) == 16.0
    await at(hass, entry, freezer, 21)
    assert target(hass, ROOM) == 16.0                     # the schedule now, no step
    assert attrs(hass, ROOM)["manual_override"]["active"] is False
    assert attrs(hass, ROOM)["preset_mode"] == "schedule"
    await at(hass, entry, freezer, 6, day=15)
    assert target(hass, ROOM) == 18.0                     # and on as usual


async def test_skip_to_tomorrows_change(hass, setup, freezer):
    entry = await setup(hour=22)
    await press(hass, ROOM_SKIP)
    assert target(hass, ROOM) == 18.0                     # tomorrow's 06:00 19 °C, - 1
    await at(hass, entry, freezer, 3, day=15)
    assert target(hass, ROOM) == 18.0
    await at(hass, entry, freezer, 6, day=15)
    assert attrs(hass, ROOM)["manual_override"]["active"] is False
    assert target(hass, ROOM) == 18.0


async def test_second_press_changes_nothing(hass, setup):
    entry = await setup()
    await press(hass, ROOM_SKIP)
    before = dict(entry.runtime_data.manual_room_temp["downstairs"]["lounge"])
    await press(hass, ROOM_SKIP)
    assert entry.runtime_data.manual_room_temp["downstairs"]["lounge"] == before
    assert target(hass, ROOM) == 16.0


async def test_room_skip_ends_its_boost(hass, setup):
    entry = await setup()
    await entry.runtime_data.set_boost("downstairs", "lounge", temperature=23.0, duration=60)
    await settle(hass)
    assert target(hass, ROOM) == 23.0
    await press(hass, ROOM_SKIP)
    assert target(hass, ROOM) == 16.0
    assert attrs(hass, ROOM)["boost"]["temperature"] is None


async def test_room_skip_service(hass, setup):
    await setup()
    await service(hass, ROOM)
    assert target(hass, ROOM) == 16.0


# ---------------------------------------------------------------------------
# Zones and the whole house
# ---------------------------------------------------------------------------

async def test_zone_skip_moves_every_room(hass, setup):
    entry = await setup()
    coordinator = entry.runtime_data
    await coordinator.set_manual_room_temperature("downstairs", "lounge", 22.0)
    await coordinator.set_boost("downstairs", "study", temperature=23.0, duration=60)
    await settle(hass)
    await press(hass, ZONE_SKIP)
    assert target(hass, ROOM) == 16.0                     # own override ended; zone's 17 - 1
    assert target(hass, STUDY) == 17.0                    # boost ended
    assert attrs(hass, ZONE)["manual_override"]["skipped"] is True
    assert coordinator.boost_manager.boost_state.get("downstairs", {}) == {}


async def test_zone_skip_service(hass, setup):
    await setup()
    await service(hass, ZONE)
    assert (target(hass, ROOM), target(hass, STUDY)) == (16.0, 17.0)


async def test_whole_house_skip_preset(hass, setup):
    entry = await setup()
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": GLOBAL, "preset_mode": "skip_to_next"}, blocking=True,
    )
    await settle(hass)
    assert (target(hass, ROOM), target(hass, STUDY)) == (16.0, 17.0)
    assert "upstairs" not in entry.runtime_data.manual_zone_temp     # nothing to skip to: left alone
    assert attrs(hass, GLOBAL)["preset_mode"] == "skip_to_next"
    # Back to the schedule
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": GLOBAL, "preset_mode": "schedule"}, blocking=True,
    )
    await settle(hass)
    assert (target(hass, ROOM), target(hass, STUDY)) == (18.0, 19.0)
    assert attrs(hass, GLOBAL)["preset_mode"] == "schedule"


async def test_whole_house_skip_service(hass, setup):
    await setup()
    await service(hass, GLOBAL)
    assert target(hass, STUDY) == 17.0


# ---------------------------------------------------------------------------
# When it can't
# ---------------------------------------------------------------------------

async def test_not_in_away_mode(hass, setup):
    entry = await setup()
    await entry.runtime_data.set_away_mode(True)
    await settle(hass)
    assert hass.states.get(ROOM_SKIP).state == "unavailable"
    assert hass.states.get(ZONE_SKIP).state == "unavailable"
    with pytest.raises(ServiceValidationError, match="Away mode"):
        await service(hass, ROOM)
    with pytest.raises(ServiceValidationError, match="Away mode"):
        await service(hass, GLOBAL)


async def test_nothing_to_skip_to(hass, setup):
    await setup()
    assert hass.states.get("button.upstairs_skip_to_next").state == "unavailable"
    with pytest.raises(ServiceValidationError, match="no schedule change"):
        await service(hass, "climate.upstairs_bedroom")


# ---------------------------------------------------------------------------
# Knock-on effects
# ---------------------------------------------------------------------------

async def test_estimates_cut_off_at_the_change_after_the_skipped_one(hass, setup):
    entry = await setup()
    coordinator = entry.runtime_data
    room_data = coordinator.data["downstairs"]["rooms"]["lounge"]
    assert coordinator._target_changes_at("downstairs", "lounge", room_data, local_dt(hour=12)) == local_dt(hour=21)
    await press(hass, ROOM_SKIP)
    room_data = coordinator.data["downstairs"]["rooms"]["lounge"]
    # 21:00 is already in force; the target next changes at tomorrow's 06:00
    assert coordinator._target_changes_at("downstairs", "lounge", room_data, local_dt(hour=12)) == local_dt(day=15, hour=6)


async def test_skip_survives_a_restart(hass, setup):
    entry = await setup()
    await press(hass, ROOM_SKIP)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert target(hass, ROOM) == 16.0
    assert attrs(hass, ROOM)["manual_override"]["skipped"] is True


# ---------------------------------------------------------------------------
# The schedule's upcoming changes
# ---------------------------------------------------------------------------

def test_upcoming_changes():
    manager = ScheduleManager(minimum_temp=10.0)
    zone = {"schedule": {
        "weekday": [{"start": "06:00", "temperature": 19.0}, {"start": "08:00", "temperature": 19.0},
                    {"start": "21:00", "temperature": 17.0}],
        "weekend": [{"start": "09:00", "temperature": 20.0}],
    }}
    friday = local_dt(2026, 1, 16, 12)
    assert manager.upcoming_changes(zone, friday, 3) == [
        (local_dt(2026, 1, 16, 21), 17.0),                 # 08:00 (same temperature) isn't a change
        (local_dt(2026, 1, 17, 9), 20.0),                  # Saturday
        (local_dt(2026, 1, 19, 6), 19.0),                  # Monday (Sunday carries Saturday's 20 °C on)
    ]
    assert manager.upcoming_changes({"schedule": {}}, friday, 1) == []
    assert manager.upcoming_changes(zone, local_dt(2026, 1, 16, 21), 1)[0][0] == local_dt(2026, 1, 17, 9)
    assert manager.upcoming_changes(zone, friday, 1) == manager.upcoming_changes(zone, friday + timedelta(seconds=30), 1)
