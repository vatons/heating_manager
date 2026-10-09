"""Tests for the "manual" preset and for switching a boosted room off.

A temperature set on a room (or zone) is reported as the "manual" preset, so
choosing "schedule" in Home Assistant's more-info dialog or a tile card's
preset buttons clears it. Before, it was reported as "schedule", so choosing
"schedule" did nothing.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from custom_components.heating_manager.const import DOMAIN

from .test_boost_entities import BOOST, BOOST_ENDS, LOUNGE_CLIMATE, boost_end, entry, press, settle  # noqa: F401

ZONE = "climate.downstairs"
GLOBAL = "climate.heating_manager"
STUDY = "climate.downstairs_study"


async def call(hass: HomeAssistant, service: str, data: dict, domain: str = "climate") -> None:
    await hass.services.async_call(domain, service, data, blocking=True)
    await settle(hass)


def attrs(hass: HomeAssistant, entity_id: str) -> dict:
    return hass.states.get(entity_id).attributes


async def test_preset_lists(hass: HomeAssistant, entry):
    assert attrs(hass, LOUNGE_CLIMATE)["preset_modes"] == ["schedule", "manual", "away", "boost"]
    assert attrs(hass, ZONE)["preset_modes"] == ["schedule", "manual", "away", "boost"]
    # The whole house has no manual temperature of its own
    assert attrs(hass, GLOBAL)["preset_modes"] == ["schedule", "away", "boost"]


async def test_manual_temperature_is_reported_as_manual(hass: HomeAssistant, entry):
    assert attrs(hass, LOUNGE_CLIMATE)["preset_mode"] == "schedule"
    await call(hass, "set_temperature", {"entity_id": LOUNGE_CLIMATE, "temperature": 21})
    assert attrs(hass, LOUNGE_CLIMATE)["preset_mode"] == "manual"
    assert attrs(hass, LOUNGE_CLIMATE)["manual_override"] == {"active": True, "temperature": 21}


async def test_choosing_schedule_clears_a_manual_temperature(hass: HomeAssistant, entry):
    await call(hass, "set_temperature", {"entity_id": LOUNGE_CLIMATE, "temperature": 21})
    await call(hass, "set_preset_mode", {"entity_id": LOUNGE_CLIMATE, "preset_mode": "schedule"})
    room = attrs(hass, LOUNGE_CLIMATE)
    assert room["preset_mode"] == "schedule"
    assert room["manual_override"]["active"] is False
    assert room["temperature"] == 19  # back to the schedule


async def test_setting_the_schedule_temperature_is_not_manual(hass: HomeAssistant, entry):
    await call(hass, "set_temperature", {"entity_id": LOUNGE_CLIMATE, "temperature": 19})
    assert attrs(hass, LOUNGE_CLIMATE)["preset_mode"] == "schedule"


async def test_choosing_manual_holds_the_current_target(hass: HomeAssistant, entry):
    await call(hass, "set_preset_mode", {"entity_id": LOUNGE_CLIMATE, "preset_mode": "manual"})
    room = attrs(hass, LOUNGE_CLIMATE)
    assert room["preset_mode"] == "manual"
    assert room["manual_override"] == {"active": True, "temperature": 19}
    assert room["temperature"] == 19


async def test_choosing_manual_again_changes_nothing(hass: HomeAssistant, entry):
    await call(hass, "set_temperature", {"entity_id": LOUNGE_CLIMATE, "temperature": 21})
    await call(hass, "set_preset_mode", {"entity_id": LOUNGE_CLIMATE, "preset_mode": "manual"})
    assert attrs(hass, LOUNGE_CLIMATE)["manual_override"] == {"active": True, "temperature": 21}


async def test_choosing_manual_while_boosted_keeps_the_temperature_and_ends_the_boost(hass: HomeAssistant, entry):
    await call(hass, "set_boost", {"entity_id": LOUNGE_CLIMATE, "temperature": 22}, domain=DOMAIN)
    assert attrs(hass, LOUNGE_CLIMATE)["preset_mode"] == "boost"
    await call(hass, "set_preset_mode", {"entity_id": LOUNGE_CLIMATE, "preset_mode": "manual"})
    room = attrs(hass, LOUNGE_CLIMATE)
    assert room["preset_mode"] == "manual"
    assert room["boost"]["temperature"] is None
    assert room["manual_override"] == {"active": True, "temperature": 22}
    assert boost_end(hass) is None


async def test_boost_and_away_win_over_manual(hass: HomeAssistant, entry):
    await call(hass, "set_temperature", {"entity_id": LOUNGE_CLIMATE, "temperature": 21})
    await call(hass, "set_mode", {"mode": "away"}, domain=DOMAIN)
    assert attrs(hass, LOUNGE_CLIMATE)["preset_mode"] == "away"
    await call(hass, "set_mode", {"mode": "schedule"}, domain=DOMAIN)
    assert attrs(hass, LOUNGE_CLIMATE)["preset_mode"] == "manual"  # still set after away
    # A boost replaces the manual temperature
    await press(hass, BOOST)
    await settle(hass)
    assert attrs(hass, LOUNGE_CLIMATE)["preset_mode"] == "boost"
    assert attrs(hass, LOUNGE_CLIMATE)["manual_override"]["active"] is False


async def test_zone_manual_preset(hass: HomeAssistant, entry):
    await call(hass, "set_temperature", {"entity_id": ZONE, "temperature": 21})
    assert attrs(hass, ZONE)["preset_mode"] == "manual"
    await call(hass, "set_preset_mode", {"entity_id": ZONE, "preset_mode": "schedule"})
    assert attrs(hass, ZONE)["preset_mode"] == "schedule"
    assert attrs(hass, ZONE)["manual_override"]["active"] is False

    await call(hass, "set_preset_mode", {"entity_id": ZONE, "preset_mode": "manual"})
    assert attrs(hass, ZONE)["preset_mode"] == "manual"
    assert attrs(hass, ZONE)["manual_override"]["active"] is True


async def test_a_room_manual_temperature_doesnt_make_the_zone_manual(hass: HomeAssistant, entry):
    await call(hass, "set_temperature", {"entity_id": LOUNGE_CLIMATE, "temperature": 21})
    assert attrs(hass, ZONE)["preset_mode"] == "schedule"


async def test_manual_has_a_name(hass: HomeAssistant, entry):
    translations = await async_get_translations(hass, "en", "entity", [DOMAIN])
    for key in ("room", "zone"):
        assert translations[f"component.{DOMAIN}.entity.climate.{key}.state_attributes.preset_mode.state.manual"] == "Manual"


# ---------------------------------------------------------------------------
# Switching a boosted room off
# ---------------------------------------------------------------------------

async def test_switching_off_ends_the_boost(hass: HomeAssistant, entry):
    await press(hass, BOOST)
    await settle(hass)
    assert boost_end(hass) is not None
    await call(hass, "set_hvac_mode", {"entity_id": LOUNGE_CLIMATE, "hvac_mode": "off"})
    room = attrs(hass, LOUNGE_CLIMATE)
    assert hass.states.get(LOUNGE_CLIMATE).state == "off"
    assert room["boost"]["temperature"] is None
    assert room["preset_mode"] == "schedule"
    assert boost_end(hass) is None
    assert attrs(hass, ZONE)["boost"]["room_ids"] == []
    # Turning it back on doesn't bring the boost back
    await call(hass, "set_hvac_mode", {"entity_id": LOUNGE_CLIMATE, "hvac_mode": "heat"})
    assert attrs(hass, LOUNGE_CLIMATE)["boost"]["temperature"] is None


async def test_switching_off_a_room_without_a_boost(hass: HomeAssistant, entry):
    await call(hass, "set_hvac_mode", {"entity_id": STUDY, "hvac_mode": "off"})
    assert hass.states.get(STUDY).state == "off"
    assert hass.states.get(LOUNGE_CLIMATE).state == "heat"


async def test_switching_off_one_room_keeps_other_boosts(hass: HomeAssistant, entry):
    await call(hass, "set_preset_mode", {"entity_id": ZONE, "preset_mode": "boost"})
    assert sorted(attrs(hass, ZONE)["boost"]["room_ids"]) == ["lounge", "study"]
    await call(hass, "set_hvac_mode", {"entity_id": STUDY, "hvac_mode": "off"})
    assert attrs(hass, ZONE)["boost"]["room_ids"] == ["lounge"]


async def test_boost_ending_on_switch_off_survives_a_restart(hass: HomeAssistant, entry):
    await press(hass, BOOST)
    await settle(hass)
    await call(hass, "set_hvac_mode", {"entity_id": LOUNGE_CLIMATE, "hvac_mode": "off"})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(LOUNGE_CLIMATE).state == "off"
    assert attrs(hass, LOUNGE_CLIMATE)["boost"]["temperature"] is None
    assert hass.states.get(BOOST_ENDS).state == "unknown"
