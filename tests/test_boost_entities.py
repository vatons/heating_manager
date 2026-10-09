"""Tests for each room's native boost entities and the climate presets' names and icons.

Every room gets, on its device next to its climate entity:
  sensor.<room>_boost_ends       when the boost ends (timestamp; unknown when not boosted)
  button.<room>_boost            boost for the room's boost duration
  button.<room>_cancel_boost     end the boost
  number.<room>_boost_duration   the room's boost duration (minutes, configuration)
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.heating_manager.const import DOMAIN
from custom_components.heating_manager.entry_data import room_unique_id

from .conftest import ALL_DAY_19, FakeTRV, entry_from_options, local_dt, set_temp
from .test_config_flow import LOUNGE, edit_zone

INTEGRATION = Path(__file__).resolve().parents[1] / "custom_components" / "heating_manager"

LOUNGE_CLIMATE = "climate.downstairs_lounge"
BOOST_ENDS = "sensor.downstairs_lounge_boost_ends"
BOOST = "button.downstairs_lounge_boost"
CANCEL = "button.downstairs_lounge_cancel_boost"
DURATION = "number.downstairs_lounge_boost_duration"
HALL_BOOST = "button.downstairs_hall_boost"
HALL_DURATION = "number.downstairs_hall_boost_duration"
STUDY_BOOST_ENDS = "sensor.downstairs_study_boost_ends"

OPTIONS = {
    "settings": {"boost_duration": 30},
    "zones": {
        "downstairs": {
            "name": "Downstairs",
            "schedule": ALL_DAY_19,
            "rooms": {
                "lounge": {"name": "Lounge", "trvs": ["climate.lounge_trv"], "sensors": [{"temperature": "sensor.lounge"}]},
                "study": {"name": "Study", "trvs": ["climate.study_trv"], "sensors": [{"temperature": "sensor.study"}]},
                # No sensors: the integration can't boost it
                "hall": {"name": "Hall", "trvs": ["climate.hall_trv"], "sensors": []},
            },
        }
    },
}


@pytest.fixture
async def entry(hass: HomeAssistant, add_trvs, freezer):
    freezer.move_to(local_dt(hour=12))
    await add_trvs(
        FakeTRV("lounge_trv", current_temperature=17.0),
        FakeTRV("study_trv", current_temperature=17.0),
        FakeTRV("hall_trv", current_temperature=17.0),
    )
    set_temp(hass, "sensor.lounge", 17.0)
    set_temp(hass, "sensor.study", 18.0)
    config_entry = entry_from_options(OPTIONS)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    for e in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(e.entry_id)


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


async def set_number(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)
    await hass.async_block_till_done()


async def settle(hass: HomeAssistant) -> None:
    """Let the coordinator's debounced refresh (at most every 10 s) run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()


def boost_end(hass: HomeAssistant, entity_id: str = BOOST_ENDS):
    state = hass.states.get(entity_id).state
    return None if state == STATE_UNKNOWN else dt_util.parse_datetime(state)


# ---------------------------------------------------------------------------
# The entities
# ---------------------------------------------------------------------------

async def test_every_room_gets_its_boost_entities(hass: HomeAssistant, entry):
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    for room_id, name in (("lounge", "Lounge"), ("study", "Study"), ("hall", "Hall")):
        climate = ent_reg.async_get(f"climate.downstairs_{room_id}")
        for entity_id, friendly in (
            (f"sensor.downstairs_{room_id}_boost_ends", "Boost ends"),
            (f"button.downstairs_{room_id}_boost", "Boost"),
            (f"button.downstairs_{room_id}_cancel_boost", "Cancel boost"),
            (f"number.downstairs_{room_id}_boost_duration", "Boost duration"),
        ):
            entity = ent_reg.async_get(entity_id)
            assert entity is not None, entity_id
            # On the room's device, in its zone's subentry, like the climate entity
            assert entity.device_id == climate.device_id
            assert entity.config_subentry_id == climate.config_subentry_id is not None
            assert entity.unique_id.startswith(f"{room_unique_id(room_id)}:")
            assert hass.states.get(entity_id).attributes["friendly_name"] == f"Downstairs {name} {friendly}"
        assert dev_reg.async_get(climate.device_id).name == f"Downstairs {name}"
    assert ent_reg.async_get(DURATION).entity_category is EntityCategory.CONFIG
    assert hass.states.get(BOOST_ENDS).attributes["device_class"] == "timestamp"
    assert hass.states.get(DURATION).attributes["unit_of_measurement"] == "min"


async def test_boost_button_and_sensor(hass: HomeAssistant, entry, freezer):
    assert boost_end(hass) is None
    await press(hass, BOOST)
    start = dt_util.utcnow()
    assert boost_end(hass) == start + timedelta(minutes=30)
    climate = hass.states.get(LOUNGE_CLIMATE).attributes
    assert climate["preset_mode"] == "boost"
    assert dt_util.parse_datetime(climate["boost"]["end_time"]) == boost_end(hass)

    await settle(hass)
    await press(hass, CANCEL)
    await settle(hass)
    assert boost_end(hass) is None
    assert hass.states.get(LOUNGE_CLIMATE).attributes["preset_mode"] == "schedule"


async def test_sensor_clears_when_the_boost_runs_out(hass: HomeAssistant, entry, freezer):
    await press(hass, BOOST)
    assert boost_end(hass) is not None
    freezer.tick(timedelta(minutes=31))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert boost_end(hass) is None


async def test_cancel_without_a_boost_does_nothing(hass: HomeAssistant, entry):
    await press(hass, CANCEL)
    assert boost_end(hass) is None
    assert hass.states.get(LOUNGE_CLIMATE).attributes["preset_mode"] == "schedule"


async def test_boost_button_turns_an_off_room_back_on(hass: HomeAssistant, entry):
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": LOUNGE_CLIMATE, "hvac_mode": "off"}, blocking=True
    )
    await settle(hass)
    assert hass.states.get(LOUNGE_CLIMATE).state == "off"
    await press(hass, BOOST)
    await settle(hass)
    assert hass.states.get(LOUNGE_CLIMATE).state == "heat"


async def test_rooms_without_sensors_cannot_be_boosted(hass: HomeAssistant, entry):
    assert hass.states.get(HALL_BOOST).state == STATE_UNAVAILABLE
    assert hass.states.get("button.downstairs_hall_cancel_boost").state == STATE_UNAVAILABLE
    assert hass.states.get(HALL_DURATION).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.downstairs_hall_boost_ends").state == STATE_UNKNOWN
    # Home Assistant skips unavailable entities in service calls
    await press(hass, HALL_BOOST)
    await settle(hass)
    assert hass.states.get("climate.downstairs_hall").attributes["preset_mode"] == "schedule"


# ---------------------------------------------------------------------------
# Boost duration
# ---------------------------------------------------------------------------

async def test_duration_starts_at_the_integration_setting(hass: HomeAssistant, entry):
    assert float(hass.states.get(DURATION).state) == 30
    assert hass.states.get(DURATION).attributes["min"] == 1
    assert hass.states.get(DURATION).attributes["max"] == 480


async def test_duration_is_used_by_the_button_preset_and_service(hass: HomeAssistant, entry):
    await set_number(hass, DURATION, 45)
    assert float(hass.states.get(DURATION).state) == 45

    await press(hass, BOOST)
    assert boost_end(hass) == dt_util.utcnow() + timedelta(minutes=45)
    await settle(hass)
    await press(hass, CANCEL)
    await settle(hass)

    # The boost preset
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": LOUNGE_CLIMATE, "preset_mode": "boost"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(LOUNGE_CLIMATE).attributes["boost"]["duration_minutes"] == 45
    await settle(hass)

    # set_boost without a duration, and with one (which wins)
    await hass.services.async_call(DOMAIN, "set_boost", {}, target={"entity_id": LOUNGE_CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(LOUNGE_CLIMATE).attributes["boost"]["duration_minutes"] == 45
    await settle(hass)
    await hass.services.async_call(
        DOMAIN, "set_boost", {"duration": 10}, target={"entity_id": LOUNGE_CLIMATE}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(LOUNGE_CLIMATE).attributes["boost"]["duration_minutes"] == 10


async def test_duration_is_per_room(hass: HomeAssistant, entry):
    await set_number(hass, DURATION, 90)
    assert float(hass.states.get("number.downstairs_study_boost_duration").state) == 30
    # Boosting the zone boosts each room for its own duration
    await hass.services.async_call(
        "climate", "set_preset_mode", {"entity_id": "climate.downstairs", "preset_mode": "boost"}, blocking=True
    )
    await hass.async_block_till_done()
    now = dt_util.utcnow()
    await settle(hass)
    assert boost_end(hass) == now + timedelta(minutes=90)
    assert boost_end(hass, STUDY_BOOST_ENDS) == now + timedelta(minutes=30)


@pytest.mark.parametrize("value", [0, 481])
async def test_duration_out_of_range_is_rejected(hass: HomeAssistant, entry, value):
    with pytest.raises(ServiceValidationError):
        await set_number(hass, DURATION, value)
    assert float(hass.states.get(DURATION).state) == 30


async def test_duration_survives_a_restart(hass: HomeAssistant, entry):
    await set_number(hass, DURATION, 75)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get(DURATION).state) == 75
    assert float(hass.states.get("number.downstairs_study_boost_duration").state) == 30


async def test_a_boost_survives_a_restart(hass: HomeAssistant, entry):
    await press(hass, BOOST)
    end = boost_end(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert boost_end(hass) == end


# ---------------------------------------------------------------------------
# Rooms added and removed
# ---------------------------------------------------------------------------

async def test_removing_a_room_removes_its_boost_entities(hass: HomeAssistant, entry):
    ent_reg = er.async_get(hass)
    await edit_zone(hass, entry, rooms=[LOUNGE])
    await hass.async_block_till_done()
    for entity_id in (BOOST_ENDS, BOOST, CANCEL, DURATION):
        assert ent_reg.async_get(entity_id) is not None, entity_id
    for entity_id in (STUDY_BOOST_ENDS, "button.downstairs_study_boost", HALL_BOOST, HALL_DURATION):
        assert ent_reg.async_get(entity_id) is None, entity_id
        assert hass.states.get(entity_id) is None


async def test_reloading_keeps_the_boost_entities(hass: HomeAssistant, entry):
    """The climate platform's clean-up only removes climate entities."""
    ent_reg = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after = {e.entity_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)}
    assert after == before
    assert {e.split(".")[0] for e in after} == {"climate", "sensor", "button", "number"}


# ---------------------------------------------------------------------------
# Names, icons and service descriptions
# ---------------------------------------------------------------------------

async def test_presets_have_names(hass: HomeAssistant, entry):
    translations = await async_get_translations(hass, "en", "entity", [DOMAIN])
    for key in ("room", "zone", "global"):
        prefix = f"component.{DOMAIN}.entity.climate.{key}.state_attributes.preset_mode.state"
        assert translations[f"{prefix}.schedule"] == "Schedule"
        assert translations[f"{prefix}.boost"] == "Boost"
        assert translations[f"{prefix}.away"] == "Away"
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get(LOUNGE_CLIMATE).translation_key == "room"
    assert ent_reg.async_get("climate.downstairs").translation_key == "zone"
    assert ent_reg.async_get("climate.heating_manager").translation_key == "global"
    # A translation key mustn't rename the climate entities: they keep their device's name
    assert hass.states.get(LOUNGE_CLIMATE).attributes["friendly_name"] == "Downstairs Lounge"
    assert hass.states.get("climate.heating_manager").attributes["friendly_name"] == "Heating Manager"


def test_icons_and_translations_cover_the_same_keys():
    icons = json.loads((INTEGRATION / "icons.json").read_text())["entity"]
    names = json.loads((INTEGRATION / "translations" / "en.json").read_text())["entity"]
    assert icons.keys() == names.keys()
    for platform in icons:
        assert icons[platform].keys() == names[platform].keys(), platform
    for key, presets in (
        ("room", {"schedule", "manual", "away", "boost"}),
        ("zone", {"schedule", "manual", "away", "boost"}),
        ("global", {"schedule", "away", "boost"}),
    ):
        preset_icons = icons["climate"][key]["state_attributes"]["preset_mode"]["state"]
        preset_names = names["climate"][key]["state_attributes"]["preset_mode"]["state"]
        assert preset_icons.keys() == preset_names.keys() == presets
        assert all(icon.startswith("mdi:") for icon in preset_icons.values())


def test_every_described_service_has_an_icon():
    services = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    icons = json.loads((INTEGRATION / "icons.json").read_text())["services"]
    registered = {"set_boost", "clear_boost", "set_mode"}
    assert set(icons) == registered
    assert registered <= set(services)


async def test_boost_services_take_a_room_target(hass: HomeAssistant, entry):
    """set_boost and clear_boost are entity services: pick rooms, not zone_id/room_id."""
    descriptions = yaml.safe_load((INTEGRATION / "services.yaml").read_text())
    for service in ("set_boost", "clear_boost"):
        assert descriptions[service]["target"] == {"entity": {"integration": DOMAIN, "domain": "climate"}}
        fields = descriptions[service].get("fields") or {}
        assert "zone_id" not in fields and "room_id" not in fields
    assert set(descriptions["set_boost"]["fields"]) == {"duration", "temperature"}
    # The documented zone_id/room_id form is rejected, as it always was
    with pytest.raises(Exception, match="zone_id"):
        await hass.services.async_call(DOMAIN, "set_boost", {"zone_id": "downstairs", "room_id": "lounge"}, blocking=True)

    await hass.services.async_call(DOMAIN, "set_boost", {"duration": 20}, target={"entity_id": LOUNGE_CLIMATE}, blocking=True)
    await hass.async_block_till_done()
    assert boost_end(hass) == dt_util.utcnow() + timedelta(minutes=20)
    await settle(hass)
    await hass.services.async_call(DOMAIN, "clear_boost", {}, target={"entity_id": LOUNGE_CLIMATE}, blocking=True)
    await settle(hass)
    assert boost_end(hass) is None
