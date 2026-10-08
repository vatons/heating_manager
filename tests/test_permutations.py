"""Configuration permutations: shared TRVs, sensorless rooms, °F installs,
missing entities and outlier sensors."""
from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heating_manager.const import DOMAIN
from custom_components.heating_manager.entry_data import (
    dedupe_trvs,
    entry_to_runtime,
    new_options,
    stored_room,
    yaml_to_options,
    zone_subentry,
)

from .conftest import FakeTRV, entry_from_options, local_dt, set_temp
from .test_config_flow import edit_zone, form, menu, open_options


def options_with_rooms(rooms: dict, schedule_temp: float = 19.0) -> dict:
    options = new_options("Home")
    zone = options["zones"]["home"]
    zone["schedule"] = {
        day: [{"start": "00:00", "end": "00:00", "temperature": schedule_temp}]
        for day in ("weekday", "weekend")
    }
    zone["rooms"] = rooms
    return options


def room(name, trvs=(), sensors=()):
    return {
        "name": name,
        "trvs": list(trvs),
        "sensors": [s if isinstance(s, dict) else {"temperature": s} for s in sensors],
    }


@pytest.fixture
async def setup_entry(hass, freezer):
    freezer.move_to(local_dt(hour=12))
    entries = []

    async def _setup(options):
        entry = options if isinstance(options, MockConfigEntry) else entry_from_options(options)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry

    yield _setup
    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)


def room_data(entry, room_id, zone_id="home"):
    return entry.runtime_data.data[zone_id]["rooms"][room_id]


# ---------------------------------------------------------------------------
# A TRV in more than one room
# ---------------------------------------------------------------------------

async def test_shared_trv_controlled_by_first_room_only(hass, add_trvs, setup_entry, caplog):
    trv = (await add_trvs(FakeTRV("shared_trv", current_temperature=17.0)))["climate.shared_trv"]
    set_temp(hass, "sensor.a", 15.0)
    set_temp(hass, "sensor.b", 22.0)
    # Stored with the TRV in both rooms (e.g. edited by hand); setup must not let both control it
    entry = await setup_entry(shared_trv_entry())
    for _ in range(3):
        await entry.runtime_data.async_refresh()
    # Only room A (cold) commands it: no more flipping between two setpoints
    assert all(sp > 17.0 for sp in trv.set_temperature_calls)
    assert room_data(entry, "b")["trvs"] == []
    assert "climate.shared_trv is already in Home / A" in caplog.text
    assert stored_rooms(entry)["b"]["trvs"] == ["climate.shared_trv"]     # stored config untouched


def stored_rooms(entry) -> dict:
    zone = next(iter(entry.subentries.values()))
    return {r["room_id"]: r for r in zone.data["rooms"]}


def shared_trv_entry() -> MockConfigEntry:
    zone = options_with_rooms({})["zones"]["home"]
    return MockConfigEntry(
        domain=DOMAIN, title="Heating Manager", data={}, version=3, options={"settings": {}},
        subentries_data=[zone_subentry("home", zone, [
            stored_room("a", room("A", ["climate.shared_trv"], ["sensor.a"])),
            stored_room("b", room("B", ["climate.shared_trv"], ["sensor.b"])),
        ])],
    )


def test_dedupe_trvs_across_zones():
    zones = {
        "z1": {"name": "Z1", "rooms": {"a": room("A", ["climate.x", "climate.y"])}},
        "z2": {"name": "Z2", "rooms": {"b": room("B", ["climate.y", "climate.z"])}},
    }
    problems = dedupe_trvs(zones)
    assert zones["z2"]["rooms"]["b"]["trvs"] == ["climate.z"]
    assert problems == ["climate.y is already in Z1 / A; not used for Z2 / B"]


def test_yaml_import_dedupes_trvs():
    options = yaml_to_options({"zones": {"z": {"rooms": {
        "a": {"trvs": ["climate.x"]}, "b": {"trvs": ["climate.x", "climate.y"]},
    }}}})
    assert options["zones"]["z"]["rooms"]["b"]["trvs"] == ["climate.y"]


def test_entry_to_runtime_does_not_modify_subentries():
    entry = shared_trv_entry()
    config, _ = entry_to_runtime(entry)
    assert config["zones"]["home"]["rooms"]["b"]["trvs"] == []
    assert stored_rooms(entry)["b"]["trvs"] == ["climate.shared_trv"]


def test_converting_options_to_subentries_dedupes_trvs():
    entry = entry_from_options(options_with_rooms({"a": room("A", ["climate.x"]), "b": room("B", ["climate.x"])}))
    rooms = stored_rooms(entry)
    assert rooms["a"]["trvs"] == ["climate.x"]
    assert rooms["b"]["trvs"] == []


async def test_room_form_rejects_trv_used_by_another_room(hass, add_trvs, setup_entry):
    await add_trvs(FakeTRV("lounge_trv"), FakeTRV("spare_trv"))
    entry = await setup_entry(options_with_rooms({"lounge": room("Lounge", ["climate.lounge_trv"])}))
    lounge = {"name": "Lounge", "trvs": ["climate.lounge_trv"], "sensors": []}
    result = await edit_zone(hass, entry, "home", rooms=[
        lounge, {"name": "Study", "trvs": ["climate.lounge_trv", "climate.spare_trv"], "sensors": []}])
    assert result["errors"] == {"base": "trv_in_use"}
    assert result["description_placeholders"]["trv_conflict"] == "climate.lounge_trv (Home › Lounge)"
    result = await edit_zone(hass, entry, "home", rooms=[
        lounge, {"name": "Study", "trvs": ["climate.spare_trv"], "sensors": []}])
    assert result["type"] is FlowResultType.ABORT


async def test_import_confirm_warns_about_shared_trvs(hass, add_trvs, setup_entry):
    await add_trvs(FakeTRV("lounge_trv"))
    entry = await setup_entry(options_with_rooms({"lounge": room("Lounge", ["climate.lounge_trv"])}))
    path = hass.config.path("hm_shared_trv.yaml")
    with open(path, "w") as fh:
        fh.write("zones:\n  upstairs:\n    rooms:\n      bed:\n        trvs: [climate.lounge_trv]\n")
    try:
        flow_id = await open_options(hass, entry)
        await menu(hass, flow_id, "import_yaml")
        result = await form(hass, flow_id, {"path": "hm_shared_trv.yaml", "import_settings": False})
    finally:
        import os
        os.remove(path)
    assert "climate.lounge_trv is already in Home / Lounge" in result["description_placeholders"]["warnings"]
    hass.config_entries.options.async_abort(flow_id)


# ---------------------------------------------------------------------------
# Rooms with TRVs but no sensors
# ---------------------------------------------------------------------------

async def test_room_without_sensors_uses_its_trv_temperature(hass, add_trvs, setup_entry):
    trv = (await add_trvs(FakeTRV("room_trv", current_temperature=16.0)))["climate.room_trv"]
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"])}))
    data = room_data(entry, "r")
    assert (data["temperature"], data["temperature_source"]) == (16.0, "trv")
    assert data["needs_heating"] is True
    assert entry.runtime_data.data["home"]["heating_demand"] is True
    assert trv.valve_open
    # The TRV can't learn an offset against itself
    assert entry.runtime_data.trv_controller.offset_ema == {}


async def test_room_without_sensors_and_no_trv_reading_uses_zone_average(hass, add_trvs, setup_entry):
    await add_trvs(FakeTRV("room_trv", current_temperature=None), FakeTRV("other_trv"))
    set_temp(hass, "sensor.other", 18.0)
    entry = await setup_entry(options_with_rooms({
        "r": room("R", ["climate.room_trv"]),
        "other": room("Other", ["climate.other_trv"], ["sensor.other"]),
    }))
    data = room_data(entry, "r")
    assert (data["temperature"], data["temperature_source"]) == (18.0, "zone_average")


async def test_room_with_offline_sensor_still_follows_fallback_mode(hass, add_trvs, setup_entry):
    """Sensorless rooms prefer the TRV; a room whose sensor is offline uses fallback_mode."""
    await add_trvs(FakeTRV("room_trv", current_temperature=16.0), FakeTRV("other_trv"))
    hass.states.async_set("sensor.room", "unavailable")
    set_temp(hass, "sensor.other", 18.0)
    entry = await setup_entry(options_with_rooms({
        "r": room("R", ["climate.room_trv"], ["sensor.room"]),
        "other": room("Other", ["climate.other_trv"], ["sensor.other"]),
    }))
    assert room_data(entry, "r")["temperature_source"] == "zone_average"


# ---------------------------------------------------------------------------
# Fahrenheit installs
# ---------------------------------------------------------------------------

@pytest.fixture
def fahrenheit(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM


def set_temp_f(hass, entity_id, fahrenheit_value):
    hass.states.async_set(entity_id, str(fahrenheit_value), {"unit_of_measurement": "°F"})


async def test_fahrenheit_install_heats_cold_room(hass, fahrenheit, add_trvs, setup_entry):
    # FakeTRV is a °C device; HA shows and accepts its temperatures in °F, rounded to
    # whole degrees (18°C shows as 64°F, i.e. 17.8°C)
    trv = (await add_trvs(FakeTRV("room_trv", current_temperature=18.0)))["climate.room_trv"]
    assert hass.states.get("climate.room_trv").attributes["current_temperature"] == 64
    set_temp_f(hass, "sensor.room", 64.4)                       # 18°C
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"], ["sensor.room"])}))

    data = room_data(entry, "r")
    assert data["temperature"] == pytest.approx(18.0)
    assert data["target_temperature"] == 19.0                   # schedule is in °C
    assert data["needs_heating"] is True
    assert trv.valve_open
    # 19 + learned offset (17.8 - 18 = -0.2) + 1.5 boost, in °C at the device
    assert trv.target_temperature == pytest.approx(20.3, abs=0.1)
    assert data["trv_offset_info"]["climate.room_trv"]["trv_internal_temp"] == pytest.approx(17.78, abs=0.01)
    # The room's own climate entity shows °F
    state = hass.states.get("climate.home_r")
    assert state.attributes["current_temperature"] == 64
    assert state.attributes["temperature"] == 66


async def test_fahrenheit_trv_limits_and_step(hass, fahrenheit, add_trvs, setup_entry):
    # A °C TRV with a 25°C max (77°F); a cold room asks for more than that
    trv = (await add_trvs(FakeTRV("room_trv", current_temperature=15.0, max_temp=25.0)))["climate.room_trv"]
    set_temp_f(hass, "sensor.room", 50.0)                       # 10°C
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"], ["sensor.room"])}))
    assert trv.target_temperature == pytest.approx(25.0)
    assert trv.valve_open


async def test_fahrenheit_set_temperature_on_room_entity(hass, fahrenheit, add_trvs, setup_entry):
    await add_trvs(FakeTRV("room_trv", current_temperature=18.0))
    set_temp_f(hass, "sensor.room", 64.4)
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"], ["sensor.room"])}))
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": "climate.home_r", "temperature": 70}, blocking=True
    )
    await entry.runtime_data.async_refresh()
    assert room_data(entry, "r")["target_temperature"] == pytest.approx(21.11, abs=0.01)


async def test_fahrenheit_boost_service(hass, fahrenheit, add_trvs, setup_entry):
    await add_trvs(FakeTRV("room_trv", current_temperature=18.0))
    set_temp_f(hass, "sensor.room", 64.4)
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"], ["sensor.room"])}))
    await hass.services.async_call(
        DOMAIN, "set_boost", {"entity_id": "climate.home_r", "temperature": 71.6}, blocking=True
    )
    await entry.runtime_data.async_refresh()
    assert room_data(entry, "r")["target_temperature"] == pytest.approx(22.0)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "set_boost", {"entity_id": "climate.home_r", "temperature": 100}, blocking=True
        )


async def test_mixed_sensor_units(hass, add_trvs, setup_entry):
    """A °F sensor in a °C install is converted, not taken as °C."""
    await add_trvs(FakeTRV("room_trv", current_temperature=18.0))
    set_temp(hass, "sensor.c", 18.0)
    set_temp_f(hass, "sensor.f", 66.2)                          # 19°C
    entry = await setup_entry(options_with_rooms({
        "r": room("R", ["climate.room_trv"], ["sensor.c", "sensor.f"]),
    }))
    assert room_data(entry, "r")["temperature"] == pytest.approx(18.5)


# ---------------------------------------------------------------------------
# Missing or disabled entities
# ---------------------------------------------------------------------------

async def test_missing_entities_repair_issue(hass, add_trvs, setup_entry, freezer):
    await add_trvs(FakeTRV("room_trv", current_temperature=18.0))
    set_temp(hass, "sensor.room", 18.0)
    entry = await setup_entry(options_with_rooms({
        "r": room("R", ["climate.room_trv", "climate.renamed_trv"],
                  ["sensor.room", {"temperature": "sensor.gone", "last_seen": "sensor.gone_last_seen"}]),
    }))
    issues = ir.async_get(hass)
    coordinator = entry.runtime_data

    async def run(minutes):
        freezer.tick(timedelta(minutes=minutes))
        set_temp(hass, "sensor.room", 18.0)
        await coordinator.async_refresh()

    await run(5)
    assert issues.async_get_issue(DOMAIN, "missing_entities") is None        # grace period
    await run(6)
    issue = issues.async_get_issue(DOMAIN, "missing_entities")
    assert issue is not None
    listed = issue.translation_placeholders["entities"]
    assert "climate.renamed_trv (TRV in Home / R): not found" in listed
    assert "sensor.gone (temperature sensor in Home / R): not found" in listed
    assert "sensor.gone_last_seen (last seen sensor in Home / R): not found" in listed
    assert "climate.room_trv" not in listed and "sensor.room (" not in listed

    # Everything comes back: the issue clears
    hass.states.async_set("climate.renamed_trv", "heat", {"current_temperature": 18})
    set_temp(hass, "sensor.gone", 18.0)
    hass.states.async_set("sensor.gone_last_seen", local_dt(hour=12).isoformat())
    await run(1)
    assert issues.async_get_issue(DOMAIN, "missing_entities") is None


async def test_unavailable_but_registered_entity_is_not_missing(hass, add_trvs, setup_entry, freezer):
    """An offline device (state unavailable) isn't flagged; that's normal, temporary."""
    trv = (await add_trvs(FakeTRV("room_trv", current_temperature=18.0)))["climate.room_trv"]
    set_temp(hass, "sensor.room", 18.0)
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"], ["sensor.room"])}))
    trv.set_available(False)
    for _ in range(3):
        freezer.tick(timedelta(minutes=6))
        set_temp(hass, "sensor.room", 18.0)
        await entry.runtime_data.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_entities") is None


async def test_disabled_entity_is_reported(hass, add_trvs, setup_entry, freezer):
    await add_trvs(FakeTRV("room_trv", current_temperature=18.0))
    set_temp(hass, "sensor.room", 18.0)
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"], ["sensor.room"])}))
    er.async_get(hass).async_update_entity(
        "climate.room_trv", disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    for _ in range(3):
        freezer.tick(timedelta(minutes=6))
        set_temp(hass, "sensor.room", 18.0)
        await entry.runtime_data.async_refresh()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "missing_entities")
    assert "climate.room_trv (TRV in Home / R): disabled" in issue.translation_placeholders["entities"]


async def test_unload_clears_missing_entities_issue(hass, add_trvs, setup_entry, freezer):
    await add_trvs(FakeTRV("room_trv"))
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.nope"])}))
    freezer.tick(timedelta(minutes=11))
    await entry.runtime_data.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_entities") is not None
    await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "missing_entities") is None


# ---------------------------------------------------------------------------
# Outlier sensors: median with 3+ sensors
# ---------------------------------------------------------------------------

async def test_median_ignores_one_outlier_sensor(hass, add_trvs, setup_entry):
    """Four sensors at ~19°C and one inside the fridge at 4°C."""
    await add_trvs(FakeTRV("room_trv", current_temperature=19.0))
    for i, value in enumerate((19.0, 19.2, 18.8, 19.1, 4.0)):
        set_temp(hass, f"sensor.s{i}", value)
    entry = await setup_entry(options_with_rooms({
        "kitchen": room("Kitchen", ["climate.room_trv"], [f"sensor.s{i}" for i in range(5)]),
    }))
    data = room_data(entry, "kitchen")
    assert data["temperature"] == pytest.approx(19.0)            # mean would be 16.0
    assert data["needs_heating"] is False


async def test_two_sensors_are_averaged(hass, add_trvs, setup_entry):
    await add_trvs(FakeTRV("room_trv", current_temperature=19.0))
    set_temp(hass, "sensor.a", 18.0)
    set_temp(hass, "sensor.b", 19.0)
    entry = await setup_entry(options_with_rooms({"r": room("R", ["climate.room_trv"], ["sensor.a", "sensor.b"])}))
    assert room_data(entry, "r")["temperature"] == pytest.approx(18.5)
