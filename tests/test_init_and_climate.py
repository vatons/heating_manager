"""Tests for integration setup and the climate entities exposed to Home Assistant."""
import pytest
import yaml

from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.heating_manager.const import DOMAIN

from .conftest import ALL_DAY_19, FakeTRV, local_dt, make_room, set_temp

ROOM = "climate.downstairs_lounge"
ROOM_B = "climate.downstairs_study"
ZONE = "climate.downstairs"
GLOBAL = "climate.heating_manager"


def write_config(tmp_path, zones=None, **top) -> str:
    cfg = {
        "minimum_temp": 10,
        "frost_protection_temp": 6,
        "zones": zones or {
            "downstairs": {
                "name": "Downstairs",
                "schedule": ALL_DAY_19,
                "rooms": {
                    "lounge": make_room("Lounge", ["climate.lounge_trv"], ["sensor.lounge"]),
                    "study": make_room("Study", ["climate.study_trv"], ["sensor.study"]),
                },
            }
        },
        **top,
    }
    path = tmp_path / "heating_manager.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


@pytest.fixture
async def setup_hm(hass: HomeAssistant, add_trvs, tmp_path, freezer):
    """Set up the integration exactly as configuration.yaml would."""
    freezer.move_to(local_dt(hour=12))

    async def _setup(config_path=None, trvs=None, temps=None, **top):
        trvs = await add_trvs(*(trvs or [
            FakeTRV("lounge_trv", current_temperature=17.0),
            FakeTRV("study_trv", current_temperature=20.0),
        ]))
        for entity_id, value in (temps or {"sensor.lounge": 17.0, "sensor.study": 20.0}).items():
            set_temp(hass, entity_id, value)
        path = config_path or write_config(tmp_path, **top)
        ok = await async_setup_component(hass, DOMAIN, {DOMAIN: {"config_file": path}})
        await hass.async_block_till_done()
        return ok, trvs

    yield _setup
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(config_entry.entry_id)


def entry(hass):
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    return entries[0]


def coord(hass):
    return entry(hass).runtime_data


async def refresh(hass):
    await coord(hass).async_refresh()
    await hass.async_block_till_done()


async def call(hass, service, data, domain=CLIMATE_DOMAIN):
    await hass.services.async_call(domain, service, data, blocking=True)
    await refresh(hass)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def test_setup_creates_entities(hass, setup_hm):
    ok, _ = await setup_hm()
    assert ok
    for entity_id in (ROOM, ROOM_B, ZONE, GLOBAL):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.services.has_service(DOMAIN, "set_mode")
    assert hass.services.has_service(DOMAIN, "set_boost")
    assert hass.services.has_service(DOMAIN, "clear_boost")


async def test_missing_config_file_fails_setup(hass, setup_hm, tmp_path):
    ok, _ = await setup_hm(config_path=str(tmp_path / "missing.yaml"))
    assert not ok


async def test_invalid_yaml_fails_setup(hass, setup_hm, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("zones: [unclosed")
    ok, _ = await setup_hm(config_path=str(bad))
    assert not ok


async def test_relative_config_path_resolved_against_config_dir(hass, add_trvs, freezer):
    import os
    freezer.move_to(local_dt(hour=12))
    await add_trvs(FakeTRV("lounge_trv"))
    rel = "hm_test_relative.yaml"
    with open(hass.config.path(rel), "w") as fh:
        yaml.safe_dump({"zones": {"z": {"rooms": {"r": make_room("R", ["climate.lounge_trv"])}}}}, fh)
    try:
        assert await async_setup_component(hass, DOMAIN, {DOMAIN: {"config_file": rel}})
    finally:
        os.remove(hass.config.path(rel))


async def test_yaml_file_settings_take_precedence(hass, setup_hm):
    await setup_hm(minimum_temp=12, heating_deadband=0.5)
    assert coord(hass).minimum_temp == 12
    assert coord(hass).heating_deadband == 0.5


# ---------------------------------------------------------------------------
# Entity state
# ---------------------------------------------------------------------------

async def test_room_state(hass, setup_hm):
    await setup_hm()
    state = hass.states.get(ROOM)
    assert state.state == HVACMode.HEAT
    assert state.attributes["current_temperature"] == 17.0
    assert state.attributes[ATTR_TEMPERATURE] == 19.0
    assert state.attributes[ATTR_HVAC_ACTION] == "heating"
    assert state.attributes[ATTR_PRESET_MODE] == "schedule"
    assert state.attributes["trv_control"]["trvs"][0]["entity_id"] == "climate.lounge_trv"
    assert hass.states.get(ROOM_B).attributes[ATTR_HVAC_ACTION] == "idle"


async def test_zone_and_global_state(hass, setup_hm):
    await setup_hm()
    zone = hass.states.get(ZONE)
    assert zone.attributes["current_temperature"] == pytest.approx(18.5)
    assert zone.attributes[ATTR_TEMPERATURE] == 19.0
    assert zone.attributes[ATTR_HVAC_ACTION] == "heating"
    assert zone.attributes["rooms_needing_heat"] == ["lounge"]
    glob = hass.states.get(GLOBAL)
    assert glob.attributes[ATTR_HVAC_ACTION] == "heating"
    assert glob.attributes["heating"]["zones_needing_heat"] == ["downstairs"]
    assert glob.attributes["trv_control"]["total_trvs"] == 2


async def test_demand_goes_idle_when_rooms_warm(hass, setup_hm, freezer):
    await setup_hm()
    freezer.tick(600)
    set_temp(hass, "sensor.lounge", 19.5)
    await refresh(hass)
    assert hass.states.get(ZONE).attributes[ATTR_HVAC_ACTION] == "idle"
    assert hass.states.get(GLOBAL).attributes[ATTR_HVAC_ACTION] == "idle"


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

async def test_set_mode_service(hass, setup_hm):
    await setup_hm()
    await call(hass, "set_mode", {"mode": "away"}, domain=DOMAIN)
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 6
    assert hass.states.get(GLOBAL).attributes[ATTR_PRESET_MODE] == "away"
    await call(hass, "set_mode", {"mode": "schedule"}, domain=DOMAIN)
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 19.0


async def test_room_set_temperature_creates_and_clears_override(hass, setup_hm):
    await setup_hm()
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM, ATTR_TEMPERATURE: 21})
    state = hass.states.get(ROOM)
    assert state.attributes[ATTR_TEMPERATURE] == 21
    assert state.attributes["manual_override"]["active"] is True
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM, ATTR_TEMPERATURE: 19})
    assert hass.states.get(ROOM).attributes["manual_override"]["active"] is False


async def test_room_set_temperature_while_boosted(hass, setup_hm):
    await setup_hm()
    await call(hass, "set_boost", {ATTR_ENTITY_ID: ROOM, "temperature": 22, "duration": 60}, domain=DOMAIN)
    assert hass.states.get(ROOM).attributes[ATTR_PRESET_MODE] == "boost"
    # Above schedule: boost kept, temperature updated
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM, ATTR_TEMPERATURE: 23})
    state = hass.states.get(ROOM)
    assert state.attributes[ATTR_PRESET_MODE] == "boost"
    assert state.attributes[ATTR_TEMPERATURE] == 23
    # Below schedule: boost cleared, manual override set
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM, ATTR_TEMPERATURE: 17})
    state = hass.states.get(ROOM)
    assert state.attributes[ATTR_PRESET_MODE] == "manual"
    assert state.attributes[ATTR_TEMPERATURE] == 17


async def test_set_and_clear_boost_services(hass, setup_hm):
    await setup_hm()
    await call(hass, "set_boost", {ATTR_ENTITY_ID: ROOM, "temperature": 22}, domain=DOMAIN)
    state = hass.states.get(ROOM)
    assert state.attributes[ATTR_TEMPERATURE] == 22
    assert state.attributes["boost"]["duration_minutes"] == 30
    assert hass.states.get(ZONE).attributes["boost"]["room_ids"] == ["lounge"]
    await call(hass, "clear_boost", {ATTR_ENTITY_ID: ROOM}, domain=DOMAIN)
    assert hass.states.get(ROOM).attributes[ATTR_PRESET_MODE] == "schedule"


async def test_room_presets(hass, setup_hm):
    await setup_hm()
    await call(hass, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: ROOM, ATTR_PRESET_MODE: "boost"})
    assert hass.states.get(ROOM).attributes[ATTR_PRESET_MODE] == "boost"
    await call(hass, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: ROOM, ATTR_PRESET_MODE: "away"})
    assert hass.states.get(ROOM).attributes[ATTR_PRESET_MODE] == "away"
    await call(hass, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: ROOM, ATTR_PRESET_MODE: "schedule"})
    state = hass.states.get(ROOM)
    assert state.attributes[ATTR_PRESET_MODE] == "schedule"
    assert state.attributes[ATTR_TEMPERATURE] == 19.0


async def test_zone_set_temperature_and_presets(hass, setup_hm):
    await setup_hm()
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM, ATTR_TEMPERATURE: 22})
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ZONE, ATTR_TEMPERATURE: 20})
    # Zone override replaces room overrides
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 20
    assert hass.states.get(ROOM_B).attributes[ATTR_TEMPERATURE] == 20
    assert hass.states.get(ZONE).attributes["manual_override"]["active"] is True

    await call(hass, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: ZONE, ATTR_PRESET_MODE: "boost"})
    assert hass.states.get(ROOM).attributes[ATTR_PRESET_MODE] == "boost"
    assert hass.states.get(ROOM_B).attributes[ATTR_PRESET_MODE] == "boost"

    await call(hass, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: ZONE, ATTR_PRESET_MODE: "schedule"})
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 19.0
    assert hass.states.get(ZONE).attributes["manual_override"]["active"] is False


async def test_global_set_temperature_and_presets(hass, setup_hm):
    await setup_hm()
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: GLOBAL, ATTR_TEMPERATURE: 21})
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 21
    assert hass.states.get(GLOBAL).attributes["manual_override"]["zones"] == ["downstairs"]
    await call(hass, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: GLOBAL, ATTR_PRESET_MODE: "away"})
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 6
    await call(hass, SERVICE_SET_PRESET_MODE, {ATTR_ENTITY_ID: GLOBAL, ATTR_PRESET_MODE: "schedule"})
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 19.0
    assert hass.states.get(GLOBAL).attributes[ATTR_PRESET_MODE] == "schedule"


async def test_room_off_sets_trvs_to_minimum(hass, setup_hm):
    _, trvs = await setup_hm()
    await hass.services.async_call(
        CLIMATE_DOMAIN, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: ROOM, "hvac_mode": "off"}, blocking=True
    )
    await refresh(hass)
    assert hass.states.get(ROOM).state == HVACMode.OFF
    assert hass.states.get(ROOM).attributes[ATTR_HVAC_ACTION] == "off"
    assert trvs["climate.lounge_trv"].target_temperature == 10


async def test_room_off_keeps_trvs_closed(hass, setup_hm):
    _, trvs = await setup_hm()
    await call(hass, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: ROOM, "hvac_mode": "off"})
    await refresh(hass)
    assert not trvs["climate.lounge_trv"].valve_open
    assert hass.states.get(ZONE).attributes["rooms_needing_heat"] == []


async def test_setting_room_to_its_displayed_target_changes_nothing(hass, setup_hm, tmp_path):
    zones = {
        "downstairs": {
            "name": "Downstairs",
            "schedule": ALL_DAY_19,
            "rooms": {
                "lounge": make_room("Lounge", ["climate.lounge_trv"], ["sensor.lounge"], temperature_offset=-1.0),
                "study": make_room("Study", ["climate.study_trv"], ["sensor.study"]),
            },
        }
    }
    await setup_hm(config_path=write_config(tmp_path, zones=zones))
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 18.0
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM, ATTR_TEMPERATURE: 18.0})
    state = hass.states.get(ROOM)
    assert state.attributes[ATTR_TEMPERATURE] == 18.0
    assert state.attributes["manual_override"]["active"] is False


async def test_unquoted_schedule_times_in_yaml_file(hass, setup_hm, tmp_path):
    """A hand-written config with unquoted times must load and schedule correctly."""
    path = tmp_path / "unquoted.yaml"
    path.write_text(
        "zones:\n"
        "  downstairs:\n"
        "    name: Downstairs\n"
        "    schedule:\n"
        "      weekday:\n"
        "        - {start: 06:30, end: 21:00, temperature: 20.5}\n"
        "      weekend:\n"
        "        - {start: 7:00, end: 23:30, temperature: 20.5}\n"
        "    rooms:\n"
        "      lounge:\n"
        "        name: Lounge\n"
        "        trvs: [climate.lounge_trv]\n"
        "        sensors: [sensor.lounge]\n"
    )
    ok, _ = await setup_hm(config_path=str(path))
    assert ok
    assert coord(hass).last_update_success
    assert hass.states.get(ROOM).attributes[ATTR_TEMPERATURE] == 20.5
    schedule = hass.states.get(ZONE).attributes["schedule"]
    assert schedule["current_period"]["end"] == "21:00"


async def test_room_with_offset_set_below_its_target_creates_override(hass, setup_hm, tmp_path):
    zones = {
        "downstairs": {
            "name": "Downstairs",
            "schedule": ALL_DAY_19,
            "rooms": {
                "lounge": make_room("Lounge", ["climate.lounge_trv"], ["sensor.lounge"], temperature_offset=-1.0),
                "study": make_room("Study", ["climate.study_trv"], ["sensor.study"]),
            },
        }
    }
    await setup_hm(config_path=write_config(tmp_path, zones=zones))
    await call(hass, SERVICE_SET_TEMPERATURE, {ATTR_ENTITY_ID: ROOM, ATTR_TEMPERATURE: 17.0})
    state = hass.states.get(ROOM)
    assert state.attributes[ATTR_TEMPERATURE] == 17.0
    assert state.attributes["manual_override"]["active"] is True


async def test_room_back_on_resumes_heating(hass, setup_hm):
    _, trvs = await setup_hm()
    await call(hass, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: ROOM, "hvac_mode": "off"})
    await call(hass, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: ROOM, "hvac_mode": "heat"})
    assert hass.states.get(ROOM).state == HVACMode.HEAT
    assert trvs["climate.lounge_trv"].valve_open
    assert hass.states.get(ZONE).attributes[ATTR_HVAC_ACTION] == "heating"


async def test_boost_switches_off_room_back_on(hass, setup_hm):
    _, trvs = await setup_hm()
    await call(hass, SERVICE_SET_HVAC_MODE, {ATTR_ENTITY_ID: ROOM, "hvac_mode": "off"})
    await call(hass, "set_boost", {ATTR_ENTITY_ID: ROOM, "temperature": 22}, domain=DOMAIN)
    assert hass.states.get(ROOM).state == HVACMode.HEAT
    assert trvs["climate.lounge_trv"].valve_open
