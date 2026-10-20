"""A room's temperature offset in everything that uses its target: zone_average demand,
TRV setpoints and held raises, analytics, the next target change, and °F installs
(offsets are entered in °C)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from .conftest import FakeTRV, local_dt, make_config, make_room, set_temp
from .test_permutations import fahrenheit, options_with_rooms, room, room_data, set_temp_f, setup_entry  # noqa: F401

ALL_DAY = lambda temp: {day: [{"start": "00:00", "temperature": temp}] for day in ("weekday", "weekend")}  # noqa: E731


@pytest.fixture
def house(hass, add_trvs, make_coordinator, freezer):
    """zone_1 from {room_id: (temperature, offset)}, one TRV each reading the room."""

    async def _build(rooms: dict, *, mode="any_room", schedule_temp=20.0, heat_source=None, schedule=None):
        freezer.move_to(local_dt(hour=12))
        trvs = await add_trvs(*[FakeTRV(f"{rid}_trv", current_temperature=temp) for rid, (temp, _) in rooms.items()])
        zone = {"name": "Zone 1", "schedule": schedule or ALL_DAY(schedule_temp), "rooms": {
            rid: make_room(rid, [f"climate.{rid}_trv"], [f"sensor.{rid}"],
                           **({"temperature_offset": offset} if offset else {}))
            for rid, (_, offset) in rooms.items()
        }}
        if heat_source:
            zone["heat_source"] = heat_source
        coordinator = make_coordinator(make_config({"zone_1": zone}, heating_demand_mode=mode))
        for rid, (temp, _) in rooms.items():
            set_temp(hass, f"sensor.{rid}", temp)
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        return coordinator, trvs

    return _build


def zone(coordinator):
    return coordinator.data["zone_1"]


def target(coordinator, rid):
    return zone(coordinator)["rooms"][rid]["target_temperature"]


# ---------------------------------------------------------------------------
# zone_average: the average target is of each room's own (offset) target
# ---------------------------------------------------------------------------

async def test_zone_average_uses_offset_targets_lower(house):
    # Targets 18 and 20 (average 19); temperatures average 19.1: no heat needed.
    # Without the offset the average target would be 20, and the zone would call.
    coordinator, _ = await house({"lounge": (18.6, -2.0), "study": (19.6, 0)}, mode="zone_average")
    assert (target(coordinator, "lounge"), target(coordinator, "study")) == (18.0, 20.0)
    assert zone(coordinator)["heating_demand"] is False


async def test_zone_average_uses_offset_targets_higher(house):
    # Targets 21 and 20 (average 20.5); temperatures average 19.8: heat needed.
    # Without the offset the average target would be 20, within the deadband.
    coordinator, _ = await house({"lounge": (19.8, 1.0), "study": (19.8, 0)}, mode="zone_average")
    assert zone(coordinator)["heating_demand"] is True


# ---------------------------------------------------------------------------
# TRVs
# ---------------------------------------------------------------------------

async def test_trv_setpoint_from_the_offset_target(house):
    coordinator, trvs = await house({"lounge": (17.0, -1.0), "study": (17.0, 0)}, schedule_temp=19.0)
    # lounge: target 18, 1 °C short: + 1.5; study: target 19, 2 °C short: + 2 x 1.5
    assert trvs["climate.lounge_trv"].target_temperature == pytest.approx(18.0 + 1.5)
    assert trvs["climate.study_trv"].target_temperature == pytest.approx(19.0 + 3.0)


async def test_raise_held_for_a_room_below_its_offset_target(house):
    """The lounge is below its own target (22) but the zone average isn't calling."""
    # Targets 22 and 20 (average 21); temperatures 20 and 22 (average 21)
    coordinator, trvs = await house({"lounge": (20.0, 2.0), "study": (22.0, 0)}, mode="zone_average")
    lounge = zone(coordinator)["rooms"]["lounge"]
    assert lounge["needs_heating"] is True
    assert zone(coordinator)["heating_demand"] is False
    assert lounge["trv_offset_info"]["climate.lounge_trv"]["held"] is True


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

async def test_log_and_estimate_use_the_offset_target(hass, house, freezer):
    hass.states.async_set("switch.boiler", "on")
    coordinator, _ = await house({"lounge": (17.0, -1.0)}, schedule_temp=19.0, heat_source="switch.boiler")
    assert target(coordinator, "lounge") == 18.0
    for _ in range(4):
        freezer.tick(timedelta(minutes=5))
        state = hass.states.get("sensor.lounge")
        hass.states.async_set(state.entity_id, state.state, state.attributes, force_update=True)
        await coordinator.async_refresh()
    hass.states.async_set("switch.boiler", "off")
    freezer.tick(timedelta(minutes=5))
    await coordinator.async_refresh()
    [entry] = coordinator.heating_analytics.get_log("zone_1", "lounge")
    assert entry["target"] == 18.0
    assert entry["climb_needed"] == 1.0


async def test_target_change_time_with_an_offset(house):
    schedule = {day: [{"start": "06:00", "temperature": 19.5}, {"start": "21:00", "temperature": 17.0}]
                for day in ("weekday", "weekend")}
    coordinator, _ = await house({"lounge": (19.0, -1.0)}, schedule=schedule)
    lounge = zone(coordinator)["rooms"]["lounge"]
    assert lounge["target_temperature"] == 18.5
    assert coordinator._target_changes_at(zone(coordinator), lounge, local_dt(hour=12)) == local_dt(hour=21)


# ---------------------------------------------------------------------------
# Fahrenheit: offsets are °C
# ---------------------------------------------------------------------------

async def test_offset_is_celsius_on_a_fahrenheit_install(hass, fahrenheit, add_trvs, setup_entry):
    await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp_f(hass, "sensor.room", 62.6)                       # 17 °C
    entry = await setup_entry(options_with_rooms(
        {"r": {**room("R", ["climate.room_trv"], ["sensor.room"]), "temperature_offset": -1.0}},
    ))
    # 19 °C - 1 °C = 18 °C, shown as 64 °F (64.4). An offset taken as °F would give 65.
    assert room_data(entry, "r")["target_temperature"] == pytest.approx(18.0)
    assert hass.states.get("climate.home_r").attributes["temperature"] == 64
