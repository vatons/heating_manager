"""Whether heat is actually going into a room: the zone's heat source entity (or, without
one, its own call for heat), the heating analytics that learn from it, and the TRV
offsets learned separately while heated and not heated (for comparison only)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant


from .conftest import ALL_DAY_19, FakeTRV, entry_from_options, local_dt, make_config, make_room, set_temp
from .test_config_flow import edit_zone, setup_entry, zones

NOON = local_dt(hour=12)
TRV = "climate.room_trv"
SENSOR = "sensor.room_temperature"
BOILER = "switch.boiler"


@pytest.fixture(autouse=True)
def _frozen(freezer):
    freezer.move_to(NOON)
    return freezer


def one_room(heat_source=None, **zone_extra):
    zone = {"name": "Zone 1", "schedule": ALL_DAY_19, "rooms": {"room": make_room("Room", [TRV], [SENSOR])}}
    if heat_source:
        zone["heat_source"] = heat_source
    zone.update(zone_extra)
    return make_config({"zone_1": zone})


async def refresh(coordinator):
    await coordinator.async_refresh()
    assert coordinator.last_update_success


def zone(coordinator):
    return coordinator.data["zone_1"]


def room(coordinator):
    return coordinator.data["zone_1"]["rooms"]["room"]


def analytics_state(coordinator, room_id="room"):
    """The stretch the analytics are in for a room: "heated" or "not_heated"."""
    return coordinator.data["zone_1"]["rooms"][room_id]["heating_analytics"]["current_state"]


# ---------------------------------------------------------------------------
# Reading the heat source
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("entity_id", "state", "attributes", "expected"),
    [
        ("switch.boiler", "on", {}, True),
        ("switch.boiler", "off", {}, False),
        ("binary_sensor.flame", "on", {}, True),
        ("input_boolean.boiler", "off", {}, False),
        ("climate.boiler", "heat", {"hvac_action": "heating"}, True),
        ("climate.boiler", "heat", {"hvac_action": "preheating"}, True),
        ("climate.boiler", "heat", {"hvac_action": "idle"}, False),
        ("climate.boiler", "heat", {}, None),             # doesn't report what it's doing
        ("switch.boiler", "unavailable", {}, None),
        ("switch.boiler", "unknown", {}, None),
        ("switch.missing", None, {}, None),
    ],
)
async def test_heat_source_state(hass, make_coordinator, entity_id, state, attributes, expected):
    if state is not None:
        hass.states.async_set(entity_id, state, attributes)
    coordinator = make_coordinator(one_room())
    assert coordinator.heat_source_state(entity_id) is expected


# ---------------------------------------------------------------------------
# Heat flowing, per zone and room
# ---------------------------------------------------------------------------

async def test_without_heat_source_uses_the_zone_demand(hass, add_trvs, make_coordinator, freezer):
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, SENSOR, 17.0)
    coordinator = make_coordinator(one_room())
    await refresh(coordinator)
    assert (zone(coordinator)["heat_flowing"], zone(coordinator)["heat_flowing_basis"]) == (True, "zone_demand")
    assert room(coordinator)["heated"] is True

    freezer.tick(timedelta(minutes=30))
    set_temp(hass, SENSOR, 20.0)                     # warm: no demand, nothing flowing
    await refresh(coordinator)
    assert zone(coordinator)["heat_flowing"] is False
    assert room(coordinator)["heated"] is False


async def test_heat_source_off_while_the_room_wants_heat(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, SENSOR, 17.0)
    hass.states.async_set(BOILER, "off")
    coordinator = make_coordinator(one_room(BOILER))
    await refresh(coordinator)
    assert zone(coordinator)["heating_demand"] is True             # asking for heat...
    assert zone(coordinator)["heat_source"] == BOILER
    assert (zone(coordinator)["heat_flowing"], zone(coordinator)["heat_flowing_basis"]) == (False, "heat_source")
    assert room(coordinator)["needs_heating"] is True
    assert room(coordinator)["heated"] is False                     # ...but none is coming
    assert analytics_state(coordinator) == "not_heated"            # learned as cooling

    hass.states.async_set(BOILER, "on")
    await refresh(coordinator)
    assert room(coordinator)["heated"] is True


async def test_unavailable_heat_source_falls_back_to_zone_demand(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, SENSOR, 17.0)
    hass.states.async_set(BOILER, "unavailable")
    coordinator = make_coordinator(one_room(BOILER))
    await refresh(coordinator)
    assert (zone(coordinator)["heat_flowing"], zone(coordinator)["heat_flowing_basis"]) == (True, "zone_demand")


async def test_room_wanting_heat_in_a_zone_that_doesnt_call(hass, add_trvs, make_coordinator):
    """zone_average: one cold room, but the zone is warm on average, so no heat flows."""
    await add_trvs(FakeTRV("cold_trv", current_temperature=17.0), FakeTRV("warm_trv", current_temperature=22.0))
    set_temp(hass, "sensor.cold", 18.5)
    set_temp(hass, "sensor.warm", 21.0)
    config = make_config({"zone_1": {
        "name": "Zone 1", "schedule": ALL_DAY_19, "heating_demand_mode": "zone_average",
        "rooms": {
            "cold": make_room("Cold", ["climate.cold_trv"], ["sensor.cold"]),
            "warm": make_room("Warm", ["climate.warm_trv"], ["sensor.warm"]),
        },
    }})
    coordinator = make_coordinator(config)
    await refresh(coordinator)
    cold = coordinator.data["zone_1"]["rooms"]["cold"]
    assert cold["needs_heating"] is True
    assert zone(coordinator)["heating_demand"] is False
    assert cold["heated"] is False
    assert analytics_state(coordinator, "cold") == "not_heated"


async def test_heat_source_listed_when_missing(hass, add_trvs, make_coordinator, freezer):
    from homeassistant.helpers import issue_registry as ir
    from custom_components.heating_manager.coordinator import MISSING_ENTITY_GRACE

    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, SENSOR, 17.0)
    coordinator = make_coordinator(one_room("switch.no_such_boiler"))
    await refresh(coordinator)
    freezer.tick(MISSING_ENTITY_GRACE + timedelta(minutes=1))
    await refresh(coordinator)
    issue = ir.async_get(hass).async_get_issue("heating_manager", "missing_entities")
    assert issue is not None
    assert "switch.no_such_boiler (heat source of Zone 1)" in issue.translation_placeholders["entities"]


# Splitting readings into heated / not heated stretches is tested in test_heating_analytics.py


# ---------------------------------------------------------------------------
# TRV offsets while heated and not heated (comparison only)
# ---------------------------------------------------------------------------

async def test_offsets_learned_separately_for_heated_and_unheated(hass, add_trvs, make_coordinator):
    trv = (await add_trvs(FakeTRV("room_trv", current_temperature=17.0)))[TRV]
    set_temp(hass, SENSOR, 17.0)
    hass.states.async_set(BOILER, "on")
    coordinator = make_coordinator(one_room(BOILER))
    await refresh(coordinator)        # first update: no previous state yet, so neither is learned
    assert coordinator.trv_controller.get_offset_by_heat("zone_1", "room", TRV) == {}

    trv.set_internal_temperature(21.0)    # hot radiator: +4
    await refresh(coordinator)
    trv.set_internal_temperature(21.0)
    await refresh(coordinator)
    hass.states.async_set(BOILER, "off")
    await refresh(coordinator)            # the boiler was on until now: still a heated reading
    trv.set_internal_temperature(18.0)    # cold radiator: +1
    await refresh(coordinator)

    by_heat = coordinator.trv_controller.get_offset_by_heat("zone_1", "room", TRV)
    assert by_heat["heated"]["samples"] == 3
    assert by_heat["heated"]["offset"] > 3.0
    assert by_heat["unheated"] == {"offset": 1.0, "samples": 1}
    # The offset used for control is unchanged: one EMA over every reading
    info = room(coordinator)["trv_offset_info"][TRV]
    assert info["offset_heated"] == by_heat["heated"]["offset"]
    assert info["offset_unheated"] == 1.0
    assert (info["samples_heated"], info["samples_unheated"]) == (3, 1)


async def test_comparison_offsets_saved_and_restored(hass, add_trvs, make_coordinator):
    trv = (await add_trvs(FakeTRV("room_trv", current_temperature=19.0)))[TRV]
    set_temp(hass, SENSOR, 17.0)
    coordinator = make_coordinator(one_room())
    await refresh(coordinator)
    await refresh(coordinator)
    await coordinator.async_save_state()
    learned = coordinator.trv_controller.get_offset_by_heat("zone_1", "room", TRV)
    assert learned["heated"]["samples"] == 1

    restarted = make_coordinator(one_room())
    await refresh(restarted)                  # loads saved state; nothing new learned on its first update
    assert restarted.trv_controller.get_offset_by_heat("zone_1", "room", TRV) == learned


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

async def test_set_heat_source_in_the_zone_form(hass, add_trvs):
    await add_trvs(FakeTRV("lounge_trv", current_temperature=17.0))
    set_temp(hass, "sensor.lounge", 17.0)
    hass.states.async_set(BOILER, "off")
    entry = await setup_entry(hass, entry_from_options({"settings": {}, "zones": {"downstairs": {
        "name": "Downstairs", "schedule": ALL_DAY_19,
        "rooms": {"lounge": make_room("Lounge", ["climate.lounge_trv"], [{"temperature": "sensor.lounge"}])},
    }}}))
    await edit_zone(hass, entry, advanced={"heat_source": BOILER, "last_seen": []})
    assert zones(entry)["downstairs"].data["heat_source"] == BOILER
    attributes = hass.states.get("climate.downstairs").attributes
    assert (attributes["heat_source"], attributes["heat_flowing"], attributes["heat_flowing_basis"]) == (
        BOILER, False, "heat_source"
    )
    assert hass.states.get("climate.downstairs_lounge").attributes["heated"] is False

    await edit_zone(hass, entry, advanced={"last_seen": []})        # cleared
    assert "heat_source" not in zones(entry)["downstairs"].data
    assert hass.states.get("climate.downstairs").attributes["heat_flowing_basis"] == "zone_demand"
    await hass.config_entries.async_unload(entry.entry_id)


def test_heat_source_from_yaml():
    from custom_components.heating_manager.entry_data import yaml_to_options

    options = yaml_to_options({"zones": {
        "a": {"heat_source": "switch.boiler", "rooms": {}},
        "b": {"heat_source": 12, "rooms": {}},
    }})
    assert options["zones"]["a"]["heat_source"] == "switch.boiler"
    assert "heat_source" not in options["zones"]["b"]
