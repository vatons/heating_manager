"""Integration tests for HeatingManagerCoordinator (the main control loop)."""
from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.heating_manager.const import DEFAULT_MAX_HEATING_DURATION, STORAGE_KEY

from .conftest import (
    ALL_DAY_19,
    FakeTRV,
    local_dt,
    make_config,
    make_room,
    set_temp,
    single_room_config,
)

NOON = local_dt(hour=12)
TRV = "climate.room_trv"
SENSOR = "sensor.room_temperature"

# 06:00-08:00 19.5, 08:00-16:00 18.0, rest falls to minimum (10)
DAY_SCHEDULE = {
    "weekday": [
        {"start": "06:00", "end": "08:00", "temperature": 19.5},
        {"start": "08:00", "end": "16:00", "temperature": 18.0},
    ],
    "weekend": [{"start": "00:00", "end": "23:59", "temperature": 19.0}],
}


@pytest.fixture(autouse=True)
def _frozen(freezer):
    freezer.move_to(NOON)
    return freezer


def room_data(coordinator, room="room", zone="zone_1"):
    return coordinator.data[zone]["rooms"][room]


async def refresh(coordinator):
    await coordinator.async_refresh()
    assert coordinator.last_update_success, "coordinator update failed"


@pytest.fixture
async def basic(hass, add_trvs, make_coordinator):
    """One room at 17°C with one TRV, schedule 19°C all day."""
    trvs = await add_trvs(FakeTRV("room_trv", current_temperature=17.0))
    set_temp(hass, SENSOR, 17.0)
    coordinator = make_coordinator(single_room_config())
    return coordinator, trvs[TRV]


# ---------------------------------------------------------------------------
# Basic control loop
# ---------------------------------------------------------------------------

async def test_cold_room_heats(basic):
    coordinator, trv = basic
    await refresh(coordinator)
    data = room_data(coordinator)
    assert data["temperature"] == 17.0
    assert data["target_temperature"] == 19.0
    assert data["needs_heating"] is True
    assert coordinator.data["zone_1"]["heating_demand"] is True
    assert trv.valve_open


async def test_warm_room_does_not_heat(hass, basic):
    coordinator, trv = basic
    set_temp(hass, SENSOR, 19.5)
    trv.set_internal_temperature(19.5)
    await refresh(coordinator)
    assert room_data(coordinator)["needs_heating"] is False
    assert coordinator.data["zone_1"]["heating_demand"] is False


async def test_overheated_room_closes_trv(hass, basic):
    coordinator, trv = basic
    set_temp(hass, SENSOR, 21.0)
    trv.set_internal_temperature(21.0)
    await refresh(coordinator)
    assert trv.target_temperature == pytest.approx(18.0)
    assert not trv.valve_open


async def test_room_without_trvs_is_reported_but_not_commanded(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, "sensor.bath", 17.0)
    cfg = make_config({"zone_1": {"schedule": ALL_DAY_19, "rooms": {
        "bath": make_room("Bathroom", sensors=["sensor.bath"]),
    }}})
    coordinator = make_coordinator(cfg)
    await refresh(coordinator)
    assert room_data(coordinator, "bath")["trvs"] == []
    assert room_data(coordinator, "bath")["needs_heating"] is True


async def test_analytics_disabled(hass, basic, make_coordinator):
    coordinator = make_coordinator(single_room_config(), analytics_enabled=False)
    await refresh(coordinator)
    assert room_data(coordinator)["heating_analytics"] is None


async def test_analytics_populated(basic):
    coordinator, _ = basic
    await refresh(coordinator)
    assert room_data(coordinator)["heating_analytics"]["samples_count"] == 1


async def test_invalid_zone_and_room_configs_are_skipped(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 18.0)
    cfg = single_room_config()
    cfg["zones"]["bad_zone"] = "oops"
    cfg["zones"]["bad_rooms"] = {"rooms": ["not", "a", "dict"]}
    cfg["zones"]["zone_1"]["rooms"]["bad_room"] = "oops"
    coordinator = make_coordinator(cfg)
    await refresh(coordinator)
    assert "bad_zone" not in coordinator.data
    assert coordinator.data["bad_rooms"]["rooms"] == {}
    assert set(coordinator.data["zone_1"]["rooms"]) == {"room"}


async def test_unavailable_room_temperature(hass, basic):
    coordinator, trv = basic
    hass.states.async_set(SENSOR, "unavailable")
    await refresh(coordinator)
    assert room_data(coordinator)["temperature"] is None
    assert room_data(coordinator)["needs_heating"] is False


# ---------------------------------------------------------------------------
# Target temperature priority
# ---------------------------------------------------------------------------

async def test_target_priority_away_boost_manual_schedule(hass, basic):
    coordinator, _ = basic
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 19.0           # schedule

    await coordinator.set_manual_zone_temperature("zone_1", 20.0)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 20.0           # manual zone

    await coordinator.set_manual_room_temperature("zone_1", "room", 21.0)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 21.0           # manual room

    await coordinator.set_boost("zone_1", "room", temperature=23.0)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 23.0           # boost
    assert room_data(coordinator)["manual_room_override"]["active"] is False

    await coordinator.set_away_mode(True)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 5.0            # away


async def test_manual_overrides_expire_when_schedule_changes(hass, add_trvs, make_coordinator, _frozen):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 18.0)
    coordinator = make_coordinator(single_room_config(schedule=DAY_SCHEDULE))
    _frozen.move_to(local_dt(hour=7))
    await refresh(coordinator)
    await coordinator.set_manual_zone_temperature("zone_1", 21.0)
    await coordinator.set_manual_room_temperature("zone_1", "room", 22.0)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 22.0

    _frozen.move_to(local_dt(hour=8, minute=1))
    set_temp(hass, SENSOR, 18.01)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 18.0
    assert coordinator.manual_room_temp == {}
    await refresh(coordinator)
    assert coordinator.manual_zone_temp == {}
    assert room_data(coordinator)["target_temperature"] == 18.0


async def test_set_manual_unknown_zone_or_room_is_ignored(basic):
    coordinator, _ = basic
    await coordinator.set_manual_room_temperature("nope", "room", 21.0)
    await coordinator.set_manual_room_temperature("zone_1", "nope", 21.0)
    await coordinator.set_manual_zone_temperature("nope", 21.0)
    assert coordinator.manual_room_temp == {} and coordinator.manual_zone_temp == {}


async def test_clear_manual_overrides(basic):
    coordinator, _ = basic
    await coordinator.set_manual_zone_temperature("zone_1", 21.0)
    await coordinator.set_manual_room_temperature("zone_1", "room", 22.0)
    await coordinator.clear_manual_room_temperature("zone_1", "room")
    await coordinator.clear_manual_zone_temperature("zone_1")
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 19.0


async def test_boost_failure_raises(basic):
    coordinator, _ = basic
    with pytest.raises(HomeAssistantError):
        await coordinator.set_boost("zone_1", "missing_room", temperature=22.0)


async def test_boost_update_and_clear(basic):
    coordinator, _ = basic
    await coordinator.set_boost("zone_1", "room", temperature=22.0)
    await coordinator.update_boost_temperature("zone_1", "room", 23.0)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 23.0
    await coordinator.clear_boost("zone_1", "room")
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 19.0


@pytest.mark.xfail(
    reason="BUG: default boost is room temp + 2°C, so boosting a cold room LOWERS its "
    "target below the schedule (services.yaml says schedule + 2°C)"
)
async def test_default_boost_never_lowers_target(hass, basic):
    coordinator, trv = basic
    set_temp(hass, SENSOR, 15.0)
    await coordinator.set_boost("zone_1", "room")
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] >= 19.0


# ---------------------------------------------------------------------------
# Room temperature offset
# ---------------------------------------------------------------------------

async def test_room_offset_applied_to_schedule(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 18.0)
    coordinator = make_coordinator(single_room_config(temperature_offset=-1.0))
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 18.0


async def test_room_offset_never_below_frost_protection(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 18.0)
    coordinator = make_coordinator(single_room_config(temperature_offset=-2.0))
    await coordinator.set_away_mode(True)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 5.0


@pytest.mark.xfail(
    reason="BUG: room temperature_offset is also applied to a room's own manual "
    "temperature, so setting the lounge to 21°C gives a target of 20°C"
)
async def test_room_offset_not_applied_to_manual_room_temperature(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 18.0)
    coordinator = make_coordinator(single_room_config(temperature_offset=-1.0))
    await coordinator.set_manual_room_temperature("zone_1", "room", 21.0)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 21.0


@pytest.mark.xfail(
    reason="BUG: room temperature_offset is also applied to an explicit boost temperature"
)
async def test_room_offset_not_applied_to_boost(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 18.0)
    coordinator = make_coordinator(single_room_config(temperature_offset=-1.0))
    await coordinator.set_boost("zone_1", "room", temperature=22.0)
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 22.0


# ---------------------------------------------------------------------------
# Zone demand
# ---------------------------------------------------------------------------

async def test_zone_demand_mode_global_and_per_zone(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("a_trv"), FakeTRV("b_trv"))
    set_temp(hass, "sensor.a", 18.5)
    set_temp(hass, "sensor.b", 19.5)
    zones = {
        "z_avg": {"schedule": ALL_DAY_19, "heating_demand_mode": "zone_average", "rooms": {
            "a": make_room("A", ["climate.a_trv"], ["sensor.a"]),
            "b": make_room("B", ["climate.b_trv"], ["sensor.b"]),
        }},
        "z_any": {"schedule": ALL_DAY_19, "rooms": {
            "a": make_room("A", [], ["sensor.a"]),
            "b": make_room("B", [], ["sensor.b"]),
        }},
    }
    coordinator = make_coordinator(make_config(zones, heating_demand_mode="any_room"))
    await refresh(coordinator)
    assert coordinator.data["z_avg"]["heating_demand"] is False     # avg 19.0, no demand
    assert coordinator.data["z_avg"]["heating_demand_mode"] == "zone_average"
    assert coordinator.data["z_any"]["heating_demand"] is True      # room a is cold


async def test_schedule_info(hass, add_trvs, make_coordinator, _frozen):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 18.0)
    coordinator = make_coordinator(single_room_config(schedule=DAY_SCHEDULE))
    _frozen.move_to(local_dt(hour=7))
    await refresh(coordinator)
    info = coordinator.data["zone_1"]["schedule_info"]
    assert info["current_temperature"] == 19.5
    assert info["current_period"]["start"] == "06:00"
    assert info["next_period"]["start"] == "08:00"

    _frozen.move_to(local_dt(hour=17))
    await refresh(coordinator)
    info = coordinator.data["zone_1"]["schedule_info"]
    assert info["current_period"] is None
    assert info["next_period"]["tomorrow"] is True


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

async def run_until_watchdog(hass, coordinator, freezer, minutes=DEFAULT_MAX_HEATING_DURATION + 5):
    """Run the loop every 5 minutes with a cold, still-reporting room sensor."""
    for i in range(minutes // 5 + 1):
        set_temp(hass, SENSOR, 17.0 + (i % 2) * 0.01)
        await coordinator.async_refresh()
        freezer.tick(timedelta(minutes=5))


async def test_watchdog_sets_trvs_low_and_clears_overrides(hass, basic, _frozen):
    coordinator, trv = basic
    await coordinator.set_boost("zone_1", "room", temperature=22.0, duration=480)
    await run_until_watchdog(hass, coordinator, _frozen)
    assert coordinator.last_update_success
    assert trv.target_temperature == coordinator.minimum_temp
    assert coordinator.boost_manager.boost_state == {}


@pytest.mark.xfail(
    reason="BUG: watchdog TRV calls are unprotected; one failing TRV fails the whole "
    "update, every cycle, freezing the boiler demand at its last value"
)
async def test_watchdog_survives_failing_trv(hass, add_trvs, make_coordinator, _frozen):
    trvs = await add_trvs(FakeTRV("room_trv", current_temperature=17.0), FakeTRV("bad_trv"))
    set_temp(hass, SENSOR, 17.0)
    coordinator = make_coordinator(single_room_config(trvs=(TRV, "climate.bad_trv")))
    trvs["climate.bad_trv"].fail_with = HomeAssistantError("zigbee timeout")
    await run_until_watchdog(hass, coordinator, _frozen)
    assert coordinator.last_update_success


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

async def test_bad_schedule_in_one_zone_does_not_break_others(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv"))
    set_temp(hass, SENSOR, 17.0)
    cfg = single_room_config()
    cfg["zones"]["zone_2"] = {
        "schedule": {"weekday": [
            {"start": "06:30", "end": "25:99", "temperature": 21},
            {"start": "06:30", "temperature": 21},
            "not a period",
        ]},
        "rooms": {"x": make_room("X", [], ["sensor.x"])},
    }
    set_temp(hass, "sensor.x", 17.0)
    coordinator = make_coordinator(cfg)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data["zone_1"]["rooms"]["room"]["needs_heating"] is True
    assert coordinator.data["zone_2"]["rooms"]["x"]["target_temperature"] == 10.0    # minimum


@pytest.mark.xfail(reason="BUG: fallback_mode is read from config but never used")
async def test_fallback_mode_trv_uses_trv_internal_temperature(hass, add_trvs, make_coordinator):
    await add_trvs(FakeTRV("room_trv", current_temperature=17.5))
    hass.states.async_set(SENSOR, "unavailable")
    coordinator = make_coordinator(single_room_config(), fallback_mode="trv")
    await refresh(coordinator)
    assert room_data(coordinator)["temperature"] == 17.5


# ---------------------------------------------------------------------------
# The reported incident
# ---------------------------------------------------------------------------

async def test_incident_cold_room_trv_with_25c_limit_opens(hass, add_trvs, make_coordinator):
    trv = (await add_trvs(
        # TRV body sits by the radiator and reads 2°C above the room sensor
        FakeTRV("room_trv", current_temperature=17.5, target_temperature=9.0, max_temp=25.0)
    ))[TRV]
    set_temp(hass, SENSOR, 15.5)
    cfg = single_room_config(schedule={
        "weekday": [{"start": "00:00", "end": "23:59", "temperature": 19.5}],
        "weekend": [{"start": "00:00", "end": "23:59", "temperature": 19.5}],
    })
    coordinator = make_coordinator(cfg)
    await refresh(coordinator)
    assert coordinator.data["zone_1"]["heating_demand"] is True
    assert trv.valve_open, f"TRV setpoint {trv.target_temperature} vs internal {trv.current_temperature}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def test_state_persists_across_restart(hass, basic, make_coordinator):
    coordinator, _ = basic
    await refresh(coordinator)
    await coordinator.set_away_mode(True)
    await coordinator.set_manual_zone_temperature("zone_1", 20.0)
    await coordinator.set_manual_room_temperature("zone_1", "room", 21.0)
    await coordinator.set_boost("zone_1", "room", temperature=23.0, duration=60)

    restarted = make_coordinator(single_room_config())
    await refresh(restarted)
    assert restarted.away_mode is True
    assert restarted.manual_zone_temp["zone_1"]["temperature"] == 20.0
    assert restarted.boost_manager.get_boost_info("zone_1", "room", NOON)["temperature"] == 23.0
    assert restarted.trv_controller._get_ema_offset("zone_1", "room", TRV) == pytest.approx(0.0)
    assert "zone_1" in restarted.heating_analytics.temp_history


async def test_loads_v1_storage(hass, hass_storage, basic, make_coordinator):
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "away_mode": False,
            "manual_zone_temp": {"zone_1": {"temperature": 20.5, "last_scheduled_temp": 19.0}},
            "room_heating_state": {"zone_1": {"room": {"previous_target": 20.5, "target_reached": True}}},
            "trv_offset_history": {"zone_1": {"room": {TRV: [1.0, 3.0]}}},
        },
    }
    coordinator = make_coordinator(single_room_config())
    await refresh(coordinator)
    assert room_data(coordinator)["target_temperature"] == 20.5
    assert "zone_1" in coordinator.trv_controller.offset_ema
