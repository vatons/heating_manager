"""After a restart, sensors from slow integrations have no state for a while. Rooms use
their readings from before the restart until then (within a grace period), instead of
falling back to the zone average, which made their graphs spike."""
from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.heating_manager.temperature_manager import MAX_RESTORED_AGE, STARTUP_GRACE

from .conftest import FakeTRV, local_dt, make_config, make_room, set_temp

NOON = local_dt(hour=12)


@pytest.fixture(autouse=True)
def _frozen(freezer):
    freezer.move_to(NOON)
    return freezer


def two_rooms():
    return make_config({"zone_1": {
        "name": "Zone 1",
        "schedule": {"weekday": [{"start": "00:00", "temperature": 19.0}],
                     "weekend": [{"start": "00:00", "temperature": 19.0}]},
        "rooms": {
            "kitchen": make_room("Kitchen", ["climate.kitchen_trv"], ["sensor.k1", "sensor.k2", "sensor.k3"]),
            "hall": make_room("Hall", ["climate.hall_trv"], ["sensor.hall"]),
        },
    }})


def room(coordinator, room_id):
    return coordinator.data["zone_1"]["rooms"][room_id]


async def run_then_restart(hass, add_trvs, make_coordinator, freezer, downtime=timedelta(minutes=2)):
    """Run with every sensor reporting, save, then 'restart' with the kitchen's sensors not loaded."""
    await add_trvs(FakeTRV("kitchen_trv", current_temperature=20.0), FakeTRV("hall_trv", current_temperature=17.0))
    for entity_id, value in (("sensor.k1", 20.0), ("sensor.k2", 20.4), ("sensor.k3", 25.0), ("sensor.hall", 16.0)):
        set_temp(hass, entity_id, value)
    before = make_coordinator(two_rooms())
    await before.async_refresh()
    assert room(before, "kitchen")["temperature"] == 20.4                   # median of the three
    await before.async_save_state()

    freezer.tick(downtime)
    for entity_id in ("sensor.k1", "sensor.k2", "sensor.k3"):
        hass.states.async_remove(entity_id)                                 # not loaded yet
    set_temp(hass, "sensor.hall", 16.0)
    after = make_coordinator(two_rooms())
    await after.async_refresh()
    return after


async def test_room_keeps_its_last_reading_while_sensors_load(hass, add_trvs, make_coordinator, freezer):
    after = await run_then_restart(hass, add_trvs, make_coordinator, freezer)
    kitchen = room(after, "kitchen")
    assert kitchen["temperature"] == 20.4                 # not the zone average (16.0 from the hall)
    assert kitchen["temperature_source"] == "restored"
    # Not learned from: it isn't a live reading
    assert after.heating_analytics.rooms["zone_1"]["kitchen"].stretch is None

    # Once the sensors load, their live readings are used
    for entity_id, value in (("sensor.k1", 20.1), ("sensor.k2", 20.2), ("sensor.k3", 20.3)):
        set_temp(hass, entity_id, value)
    freezer.tick(timedelta(minutes=1))
    await after.async_refresh()
    assert room(after, "kitchen")["temperature"] == 20.2
    assert room(after, "kitchen")["temperature_source"] == "local_sensors"


async def test_unavailable_sensors_also_use_their_last_reading(hass, add_trvs, make_coordinator, freezer):
    await add_trvs(FakeTRV("kitchen_trv"), FakeTRV("hall_trv"))
    for entity_id, value in (("sensor.k1", 20.0), ("sensor.k2", 20.4), ("sensor.k3", 25.0), ("sensor.hall", 16.0)):
        set_temp(hass, entity_id, value)
    before = make_coordinator(two_rooms())
    await before.async_refresh()
    await before.async_save_state()
    for entity_id in ("sensor.k1", "sensor.k2", "sensor.k3"):
        hass.states.async_set(entity_id, "unavailable")
    after = make_coordinator(two_rooms())
    await after.async_refresh()
    assert room(after, "kitchen")["temperature_source"] == "restored"


async def test_after_the_grace_period_the_usual_fallback_applies(hass, add_trvs, make_coordinator, freezer):
    after = await run_then_restart(hass, add_trvs, make_coordinator, freezer)
    freezer.tick(STARTUP_GRACE + timedelta(minutes=1))
    set_temp(hass, "sensor.hall", 16.0)
    await after.async_refresh()
    kitchen = room(after, "kitchen")
    assert kitchen["temperature_source"] == "zone_average"      # sensors never came back
    assert kitchen["temperature"] == 16.0


async def test_readings_from_long_ago_arent_used(hass, add_trvs, make_coordinator, freezer):
    after = await run_then_restart(
        hass, add_trvs, make_coordinator, freezer, downtime=MAX_RESTORED_AGE + timedelta(hours=1)
    )
    assert room(after, "kitchen")["temperature_source"] == "zone_average"


async def test_loaded_but_stale_sensor_isnt_restored(hass, add_trvs, make_coordinator, freezer):
    """A sensor that has loaded but stopped reporting is genuinely stale."""
    from homeassistant.core import State

    after = await run_then_restart(hass, add_trvs, make_coordinator, freezer)
    old = NOON - timedelta(hours=2)
    for entity_id in ("sensor.k1", "sensor.k2", "sensor.k3"):
        hass.states.async_set(entity_id, "20.0", {"unit_of_measurement": "°C"})
        state = hass.states.get(entity_id)
        hass.states._states[entity_id] = State(
            entity_id, state.state, state.attributes, last_changed=old, last_updated=old, last_reported=old,
        )
    await after.async_refresh()
    assert room(after, "kitchen")["temperature_source"] == "zone_average"


def test_storage_round_trip(hass):
    from homeassistant.util import dt as dt_util

    from custom_components.heating_manager.temperature_manager import TemperatureManager

    manager = TemperatureManager(hass)
    manager.last_sensor_values = {"sensor.a": {"value": 19.5, "timestamp": dt_util.utcnow()}}
    restored = TemperatureManager(hass)
    restored.restore_values(manager.values_for_storage())
    assert restored.restored_values["sensor.a"]["value"] == 19.5
    restored.restore_values({"sensor.bad": {"value": "x", "timestamp": "nope"}, "sensor.worse": 3})
    assert set(restored.restored_values) == {"sensor.a"}
