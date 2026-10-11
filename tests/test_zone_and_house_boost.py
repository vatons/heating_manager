"""set_boost / clear_boost on a zone or the whole house, the zone "manual" preset with
room offsets, and boost durations of deleted rooms."""
from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.heating_manager.const import DOMAIN

from .conftest import ALL_DAY_19, FakeTRV, entry_from_options, local_dt, set_temp
from .test_boost_entities import OPTIONS, entry, settle  # noqa: F401

ZONE = "climate.downstairs"
HOUSE = "climate.heating_manager"
ROOMS = ("climate.downstairs_lounge", "climate.downstairs_study", "climate.downstairs_hall")


async def boost(hass: HomeAssistant, entity_id: str, service: str = "set_boost", **data) -> None:
    await hass.services.async_call(DOMAIN, service, {"entity_id": entity_id, **data}, blocking=True)
    await settle(hass)


def boosted(hass: HomeAssistant) -> dict[str, float | None]:
    return {
        room: hass.states.get(room).attributes["boost"]["temperature"]
        for room in ROOMS
    }


@pytest.mark.parametrize("target", [ZONE, HOUSE])
async def test_boost_a_zone_or_the_house(hass: HomeAssistant, entry, target):
    await boost(hass, target, duration=45, temperature=22)
    # Every room with sensors is boosted; the hall has none, so it can't be
    assert boosted(hass) == {
        "climate.downstairs_lounge": 22.0, "climate.downstairs_study": 22.0, "climate.downstairs_hall": None,
    }
    lounge = hass.states.get("climate.downstairs_lounge").attributes["boost"]
    assert lounge["duration_minutes"] == 45

    await boost(hass, target, "clear_boost")
    assert set(boosted(hass).values()) == {None}


async def test_zone_boost_defaults_per_room(hass: HomeAssistant, entry):
    """No temperature: each room gets its own default; no duration: each room's own duration."""
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.downstairs_study_boost_duration", "value": 90}, blocking=True
    )
    await boost(hass, ZONE)
    lounge = hass.states.get("climate.downstairs_lounge").attributes["boost"]
    study = hass.states.get("climate.downstairs_study").attributes["boost"]
    assert (lounge["duration_minutes"], study["duration_minutes"]) == (30, 90)
    assert lounge["temperature"] == 21.0         # 19 °C schedule + 2
    assert study["temperature"] == 21.0


async def test_zone_boost_checks_the_temperature(hass: HomeAssistant, entry):
    with pytest.raises(ServiceValidationError, match="between 5 and 30 °C"):
        await boost(hass, ZONE, temperature=40)
    assert set(boosted(hass).values()) == {None}


async def test_zone_boost_in_fahrenheit(hass: HomeAssistant, entry):
    hass.config.units = US_CUSTOMARY_SYSTEM
    await boost(hass, HOUSE, temperature=71.6)
    assert hass.states.get("climate.downstairs_lounge").attributes["boost"]["temperature"] == pytest.approx(22.0)


async def test_zone_without_boostable_rooms(hass: HomeAssistant, add_trvs, freezer):
    freezer.move_to(local_dt(hour=12))
    await add_trvs(FakeTRV("hall_trv", current_temperature=17.0))
    config_entry = entry_from_options({"settings": {}, "zones": {"downstairs": {
        "name": "Downstairs", "schedule": ALL_DAY_19,
        "rooms": {"hall": {"name": "Hall", "trvs": ["climate.hall_trv"], "sensors": []}},
    }}})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="Downstairs has no rooms that can be boosted"):
        await boost(hass, ZONE)
    await boost(hass, ZONE, "clear_boost")                       # nothing to end: no error
    await hass.config_entries.async_unload(config_entry.entry_id)


# ---------------------------------------------------------------------------
# "manual" on a zone whose rooms have offsets
# ---------------------------------------------------------------------------

@pytest.fixture
async def offset_entry(hass: HomeAssistant, add_trvs, freezer):
    freezer.move_to(local_dt(hour=12))
    await add_trvs(FakeTRV("lounge_trv", current_temperature=17.0), FakeTRV("study_trv", current_temperature=17.0))
    set_temp(hass, "sensor.lounge", 17.0)
    set_temp(hass, "sensor.study", 17.0)
    config_entry = entry_from_options({"settings": {}, "zones": {"downstairs": {
        "name": "Downstairs", "schedule": ALL_DAY_19,
        "rooms": {
            "lounge": {"name": "Lounge", "trvs": ["climate.lounge_trv"], "sensors": [{"temperature": "sensor.lounge"}]},
            "study": {"name": "Study", "trvs": ["climate.study_trv"], "sensors": [{"temperature": "sensor.study"}],
                      "temperature_offset": -1.0},
        },
    }}})
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)


def targets(hass: HomeAssistant) -> tuple:
    return tuple(
        hass.states.get(e).attributes["temperature"]
        for e in (ZONE, "climate.downstairs_lounge", "climate.downstairs_study")
    )


async def test_zone_manual_preset_keeps_room_offsets(hass: HomeAssistant, offset_entry):
    before = targets(hass)
    assert before == (18.5, 19.0, 18.0)
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": ZONE, "preset_mode": "manual"}, blocking=True
    )
    await settle(hass)
    assert hass.states.get(ZONE).attributes["preset_mode"] == "manual"
    assert targets(hass) == before                                 # held, not lowered by the offset


# ---------------------------------------------------------------------------
# Boost durations of deleted rooms
# ---------------------------------------------------------------------------

async def test_deleted_rooms_boost_durations_are_dropped(hass: HomeAssistant, add_trvs, freezer, hass_storage):
    freezer.move_to(local_dt(hour=12))
    hass_storage["heating_manager.storage"] = {
        "version": 1, "minor_version": 1, "key": "heating_manager.storage",
        "data": {"version": 2, "boost_durations": {
            "downstairs": {"lounge": 45, "gone": 60},
            "old_zone": {"x": 10},
        }},
    }
    await add_trvs(FakeTRV("lounge_trv"), FakeTRV("study_trv"), FakeTRV("hall_trv"))
    config_entry = entry_from_options(OPTIONS)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.runtime_data.boost_durations == {"downstairs": {"lounge": 45}}
    assert hass.states.get("number.downstairs_lounge_boost_duration").state == "45"
    await hass.config_entries.async_unload(config_entry.entry_id)
