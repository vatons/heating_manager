"""The outdoor temperature setting, and what the coordinator feeds the analytics."""
from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.data_entry_flow import FlowResultType

from .conftest import FakeTRV, entry_from_options, local_dt, set_temp, single_room_config
from .test_config_flow import base_options, env, form, menu, open_options, setup_entry, shown  # noqa: F401

NOON = local_dt(hour=12)
SENSOR = "sensor.room_temperature"


@pytest.fixture(autouse=True)
def _frozen(freezer):
    freezer.move_to(NOON)
    return freezer


async def refresh(coordinator):
    await coordinator.async_refresh()
    assert coordinator.last_update_success


@pytest.mark.parametrize(
    ("entity_id", "state", "attributes", "expected"),
    [
        ("sensor.outside", "4.5", {"unit_of_measurement": "°C"}, 4.5),
        ("sensor.outside", "41", {"unit_of_measurement": "°F"}, 5.0),
        ("weather.home", "cloudy", {"temperature": 6.0, "temperature_unit": "°C"}, 6.0),
        ("weather.home", "cloudy", {"temperature": 50.0, "temperature_unit": "°F"}, 10.0),
        ("weather.home", "cloudy", {}, None),
        ("sensor.outside", "unavailable", {}, None),
        ("sensor.outside", "n/a", {}, None),
        ("sensor.missing", None, {}, None),
    ],
)
async def test_outdoor_temperature(hass, add_trvs, make_coordinator, entity_id, state, attributes, expected):
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, SENSOR, 17.0)
    if state is not None:
        hass.states.async_set(entity_id, state, attributes)
    coordinator = make_coordinator(single_room_config(), outdoor_temperature_entity=entity_id)
    await refresh(coordinator)
    if expected is None:
        assert coordinator.outdoor_temperature is None
    else:
        assert coordinator.outdoor_temperature == pytest.approx(expected)


async def test_no_outdoor_temperature_by_default(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, SENSOR, 17.0)
    coordinator = make_coordinator(single_room_config())
    await refresh(coordinator)
    assert coordinator.outdoor_temperature is None


async def test_fallback_temperatures_arent_learned_from(hass, add_trvs, make_coordinator):
    """A room without sensors of its own uses its TRV's temperature: not learned from."""
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    coordinator = make_coordinator(single_room_config(sensors=()))
    await refresh(coordinator)
    room = coordinator.data["zone_1"]["rooms"]["room"]
    assert room["temperature_source"] == "trv"
    assert room["heated"] is True
    assert room["heating_analytics"]["current_state"] is None       # no stretch: nothing learned


async def test_outdoor_temperature_setting(hass, env):
    entry = await setup_entry(hass, entry_from_options(base_options()))
    hass.states.async_set("sensor.outside", "3.0", {"unit_of_measurement": "°C", "device_class": "temperature"})

    flow_id = await open_options(hass, entry)
    result = await menu(hass, flow_id, "settings")
    values = shown(result)
    assert "outdoor_temperature" not in values["sensors"]           # empty until set
    values["sensors"]["outdoor_temperature"] = "sensor.outside"
    result = await form(hass, flow_id, values)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["settings"]["outdoor_temperature"] == "sensor.outside"
    assert entry.runtime_data.outdoor_temperature == 3.0
    assert hass.states.get("climate.heating_manager").attributes["outdoor_temperature"] == 3.0

    # Shown when set, and cleared by leaving it empty
    flow_id = await open_options(hass, entry)
    result = await menu(hass, flow_id, "settings")
    sensors_section = dict(result["data_schema"].schema)["sensors"]
    field = next(key for key in sensors_section.schema.schema if str(key) == "outdoor_temperature")
    assert field.description == {"suggested_value": "sensor.outside"}
    values = shown(result)
    assert "outdoor_temperature" not in values["sensors"]             # left empty: cleared
    await form(hass, flow_id, values)
    await hass.async_block_till_done()
    assert entry.options["settings"]["outdoor_temperature"] == ""
    assert entry.runtime_data.outdoor_temperature is None


def test_outdoor_temperature_from_yaml():
    from custom_components.heating_manager.entry_data import yaml_to_options

    assert yaml_to_options({"outdoor_temperature": "weather.home", "zones": {}})["settings"][
        "outdoor_temperature"] == "weather.home"
    assert yaml_to_options({"outdoor_temperature": 12, "zones": {}})["settings"]["outdoor_temperature"] == ""
