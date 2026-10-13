"""TRV commands are only sent when they change something: an unchanged setpoint isn't
sent again (except every RESEND_INTERVAL), and while no heat can reach a room a raise
(opening the valve) is held back. Lowering is always sent."""
from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.components.climate import HVACMode

from custom_components.heating_manager.trv_controller import RESEND_INTERVAL

from .conftest import FakeTRV, local_dt, make_config, make_room, set_temp

ALL_DAY_20 = {"weekday": [{"start": "00:00", "temperature": 20.0}],
              "weekend": [{"start": "00:00", "temperature": 20.0}]}


@pytest.fixture
def house(hass, add_trvs, make_coordinator, freezer):
    """zone_1 with rooms {room_id: temperature}, one TRV each (reading the same as the room)."""

    async def _build(rooms: dict, *, mode="zone_average", heat_source=None, trv_step=0.5, **trv_kwargs):
        freezer.move_to(local_dt(hour=12))
        trvs = await add_trvs(*[
            FakeTRV(f"{rid}_trv", current_temperature=temp, target_temperature_step=trv_step, **trv_kwargs)
            for rid, temp in rooms.items()
        ])
        zone = {"name": "Zone 1", "schedule": ALL_DAY_20, "rooms": {
            rid: make_room(rid, [f"climate.{rid}_trv"], [f"sensor.{rid}"]) for rid in rooms
        }}
        if heat_source:
            zone["heat_source"] = heat_source
        coordinator = make_coordinator(make_config({"zone_1": zone}, heating_demand_mode=mode))

        def set_room(rid, temp):
            set_temp(hass, f"sensor.{rid}", temp)
            trvs[f"climate.{rid}_trv"].set_internal_temperature(temp)

        async def tick(minutes=1):
            freezer.tick(timedelta(minutes=minutes))
            for rid in rooms:       # sensors report again, unchanged
                state = hass.states.get(f"sensor.{rid}")
                hass.states.async_set(state.entity_id, state.state, state.attributes, force_update=True)
            await coordinator.async_refresh()
            assert coordinator.last_update_success

        for rid, temp in rooms.items():
            set_room(rid, temp)
        await coordinator.async_refresh()
        return coordinator, trvs, set_room, tick

    return _build


def trv(trvs, rid):
    return trvs[f"climate.{rid}_trv"]


def held(coordinator, rid):
    return coordinator.data["zone_1"]["rooms"][rid]["trv_offset_info"][f"climate.{rid}_trv"]["held"]


# ---------------------------------------------------------------------------
# Unchanged setpoints aren't sent again
# ---------------------------------------------------------------------------

async def test_unchanged_setpoint_isnt_sent_every_update(house):
    coordinator, trvs, set_room, tick = await house({"lounge": 17.0}, mode="any_room")
    calls = trv(trvs, "lounge").set_temperature_calls
    assert len(calls) == 1                      # first update: sent
    for _ in range(10):
        await tick()
    assert len(calls) == 1                      # same setpoint: not sent again


async def test_unchanged_setpoint_is_sent_again_now_and_then(house):
    coordinator, trvs, set_room, tick = await house({"lounge": 17.0}, mode="any_room")
    calls = trv(trvs, "lounge").set_temperature_calls
    for _ in range(int(RESEND_INTERVAL.total_seconds() // 60) - 1):
        await tick()
    assert len(calls) == 1
    await tick(2)
    assert len(calls) == 2                      # in case a command was lost
    assert calls[0] == calls[1]


async def test_changed_setpoint_is_sent(house):
    coordinator, trvs, set_room, tick = await house({"lounge": 17.0}, mode="any_room")
    calls = trv(trvs, "lounge").set_temperature_calls
    set_room("lounge", 19.6)                   # nearly there: a smaller boost
    await tick(10)
    assert len(calls) >= 2 and calls[-1] < calls[0]


async def test_trv_changed_by_hand_is_put_back(house):
    coordinator, trvs, set_room, tick = await house({"lounge": 17.0}, mode="any_room")
    lounge = trv(trvs, "lounge")
    wanted = lounge.target_temperature
    lounge._attr_target_temperature = 12.0      # someone turned the dial
    lounge.async_write_ha_state()
    await tick()
    assert lounge.target_temperature == wanted


async def test_sent_again_after_a_restart(house, make_coordinator):
    """What was sent isn't saved: each TRV gets its setpoint once after a restart."""
    coordinator, trvs, set_room, tick = await house({"lounge": 17.0}, mode="any_room")
    calls = trv(trvs, "lounge").set_temperature_calls
    after = make_coordinator(coordinator.config)
    await after.async_refresh()
    assert len(calls) == 2 and calls[0] == calls[1]
    await after.async_refresh()
    assert len(calls) == 2


async def test_room_switched_off_gets_minimum_once(house):
    coordinator, trvs, set_room, tick = await house({"lounge": 17.0}, mode="any_room")
    calls = trv(trvs, "lounge").set_temperature_calls
    await coordinator.set_room_off("zone_1", "lounge", True)
    await tick()
    count = len(calls)
    assert calls[-1] == coordinator.minimum_temp
    for _ in range(5):
        await tick()
    assert len(calls) == count


# ---------------------------------------------------------------------------
# Raises held back while no heat can reach the room
# ---------------------------------------------------------------------------

async def test_cold_room_outvoted_in_zone_average_isnt_opened(house):
    """The zone average is above target, so the zone doesn't call and no heat flows:
    the cold room's TRV isn't raised (it would only cost battery)."""
    rooms = {"lounge": 21.5, "study": 21.5, "hall": 20.0}
    coordinator, trvs, set_room, tick = await house(rooms)
    assert coordinator.data["zone_1"]["heating_demand"] is False
    hall = trv(trvs, "hall")
    before = hall.target_temperature
    set_room("hall", 18.0)
    await tick(10)
    assert coordinator.data["zone_1"]["rooms"]["hall"]["needs_heating"] is True
    assert coordinator.data["zone_1"]["heating_demand"] is False
    assert hall.target_temperature == before
    assert held(coordinator, "hall") is True

    # The zone starts calling: the raise goes out in the same update
    for rid in ("lounge", "study"):
        set_room(rid, 19.0)
    await tick(10)
    assert coordinator.data["zone_1"]["heating_demand"] is True
    assert hall.target_temperature > before and hall.valve_open
    assert held(coordinator, "hall") is False


async def test_heat_from_a_shared_boiler_opens_the_valve(hass, house):
    """The zone isn't calling, but its heat source is on (another zone fired the boiler):
    heat is flowing, so the cold room's TRV is raised."""
    hass.states.async_set("switch.boiler", "on")
    rooms = {"lounge": 21.5, "study": 21.5, "hall": 20.0}
    coordinator, trvs, set_room, tick = await house(rooms, heat_source="switch.boiler")
    set_room("hall", 18.0)
    await tick(10)
    assert coordinator.data["zone_1"]["heating_demand"] is False
    assert trv(trvs, "hall").valve_open
    assert held(coordinator, "hall") is False


async def test_zone_calling_with_heat_source_still_off_sends(hass, house):
    """A boiler automation may wait for the TRVs to ask for heat: while the zone calls,
    setpoints go out even though the heat source is off."""
    hass.states.async_set("switch.boiler", "off")
    coordinator, trvs, set_room, tick = await house({"hall": 17.0}, mode="any_room", heat_source="switch.boiler")
    assert coordinator.data["zone_1"]["heating_demand"] is True
    assert coordinator.data["zone_1"]["heat_flowing"] is False
    assert trv(trvs, "hall").valve_open


async def test_lowering_is_sent_while_no_heat_flows(house):
    rooms = {"lounge": 21.5, "study": 21.5, "hall": 20.0}
    coordinator, trvs, set_room, tick = await house(rooms)
    hall = trv(trvs, "hall")
    hall._attr_target_temperature = 25.0        # left high from earlier heating
    hall.async_write_ha_state()
    await tick()
    assert coordinator.data["zone_1"]["heating_demand"] is False
    assert hall.target_temperature < 25.0
    assert held(coordinator, "hall") is False


async def test_trv_that_is_off_isnt_switched_on_while_no_heat_flows(house):
    rooms = {"lounge": 21.5, "study": 21.5, "hall": 18.0}
    coordinator, trvs, set_room, tick = await house(rooms, hvac_mode=HVACMode.OFF)
    hall = trv(trvs, "hall")
    assert hall.hvac_mode_calls == []
    assert held(coordinator, "hall") is True
    for rid in ("lounge", "study"):
        set_room(rid, 18.0)
    await tick(10)
    assert hall.hvac_mode_calls == [HVACMode.HEAT]
    assert hall.valve_open


async def test_held_trv_not_sent_again_and_again(house):
    rooms = {"lounge": 21.5, "study": 21.5, "hall": 18.0}
    coordinator, trvs, set_room, tick = await house(rooms)
    calls = trv(trvs, "hall").set_temperature_calls
    count = len(calls)
    for _ in range(int(RESEND_INTERVAL.total_seconds() // 60) + 5):
        await tick()
    assert len(calls) == count
    assert held(coordinator, "hall") is True


async def test_held_shown_in_the_room_attributes(hass, house):
    rooms = {"lounge": 21.5, "study": 21.5, "hall": 18.0}
    coordinator, trvs, set_room, tick = await house(rooms)
    from custom_components.heating_manager.climate import RoomClimate

    entity = RoomClimate(coordinator, "zone_1", "hall", coordinator.config["zones"]["zone_1"]["rooms"]["hall"])
    entity.hass = hass
    [entry] = entity.extra_state_attributes["trv_control"]["trvs"]
    assert entry["held"] is True
