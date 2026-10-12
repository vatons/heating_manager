"""The log of heated stretches: what each started from (a cold or already hot system, how
many TRVs shared the heat, how long since the room was last heated) and what it showed."""
from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.heating_manager.heating_analytics import LOG_SIZE, HeatingAnalytics

from .conftest import ALL_DAY_19, FakeTRV, local_dt, make_config, make_room, set_temp
from .test_config_flow import env  # noqa: F401
from .test_heating_analytics import MINUTE, T0, Room

NOON = local_dt(hour=12)


@pytest.fixture
def analytics():
    return HeatingAnalytics(min_stretches=1, learning_rate=0.3)


def context(**values):
    return {"target": 21.0, "trvs_heating": 2, "zone_heat_minutes": 0.0, "house_heat_minutes": 0.0, **values}


def feed(analytics, start, minutes, temp, heated, rate=0.0, outdoor=None, ctx=None):
    for minute in range(minutes):
        analytics.record_temperature(
            "z", "r", round(temp + rate * minute / 60, 2), heated, start + minute * MINUTE,
            outdoor_temp=outdoor, context=ctx,
        )
    return start + minutes * MINUTE


# ---------------------------------------------------------------------------
# HeatingAnalytics
# ---------------------------------------------------------------------------

def test_heated_stretch_is_logged_with_what_it_started_from(analytics):
    now = feed(analytics, T0, 20, 18.0, False, outdoor=4.0)
    # 10 minutes' warm-up, then 1 °C/h
    for minute in range(70):
        temp = 18.0 + max(0, minute - 10) / 60
        analytics.record_temperature(
            "z", "r", round(temp, 2), True, now + minute * MINUTE, outdoor_temp=4.0 + minute / 69,
            context=context(trvs_heating=3 if minute < 35 else 5),
        )
    feed(analytics, now + 70 * MINUTE, 2, 19.0, False)
    [entry] = analytics.get_log("z", "r")
    assert entry["start"] == now.isoformat()
    assert entry["minutes"] == 69.0
    assert entry["continued"] is False
    assert entry["start_temp"] == 18.0
    assert entry["end_temp"] == pytest.approx(18.98, abs=0.01)
    assert entry["target"] == 21.0
    assert entry["climb_needed"] == 3.0
    assert entry["outdoor"] == 4.5
    assert entry["warm_up_minutes"] == pytest.approx(10, abs=1)
    assert entry["rate"] == pytest.approx(1.0, abs=0.05)
    assert entry["zone_heat_minutes"] == 0.0
    assert entry["house_heat_minutes"] == 0.0
    assert entry["trvs_heating"] == 3                  # at the start
    assert entry["trvs_heating_mean"] == 4.0           # through the stretch
    assert entry["minutes_since_heated"] is None       # never heated before


def test_minutes_since_the_room_was_last_heated(analytics):
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, ctx=context())
    now = feed(analytics, now, 45, 18.5, False)
    feed(analytics, now, 30, 18.2, True, rate=1.0, ctx=context(zone_heat_minutes=12.5))
    feed(analytics, now + 30 * MINUTE, 2, 18.7, False)
    first, second = analytics.get_log("z", "r")
    assert first["minutes_since_heated"] is None
    assert second["minutes_since_heated"] == 46.0      # last heated reading to this start
    assert second["zone_heat_minutes"] == 12.5         # joined heat already flowing


def test_stretch_without_a_rise_is_logged_too(analytics):
    """A room that got heat but didn't warm: worth knowing, though nothing is learned."""
    now = feed(analytics, T0, 40, 18.0, True, ctx=context())
    feed(analytics, now, 2, 18.0, False)
    [entry] = analytics.get_log("z", "r")
    assert entry["rate"] is None and entry["warm_up_minutes"] is None
    assert analytics.rooms["z"]["r"].heating_stretches == 0


def test_long_stretch_logged_in_parts(analytics):
    room = Room(analytics)
    room.run(4 * 60, rate=0.5).run(2, heated=False)
    first, second = analytics.get_log("z", "r")
    assert first["continued"] is False
    assert second["continued"] is True
    assert second["minutes_since_heated"] == 0
    assert second["warm_up_minutes"] is None            # already warming


def test_log_is_kept_to_its_size(analytics):
    now = T0
    for _ in range(LOG_SIZE + 5):
        now = feed(analytics, now, 3, 18.0, True, ctx=context())
        now = feed(analytics, now, 3, 18.0, False)
    log = analytics.get_log("z", "r")
    assert len(log) == LOG_SIZE
    assert log[-1]["start"] == (now - 6 * MINUTE).isoformat()


def test_log_survives_a_restart(analytics):
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, ctx=context())
    feed(analytics, now, 2, 18.5, False)
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_history(analytics.get_history_for_storage())
    assert restored.get_log("z", "r") == analytics.get_log("z", "r")
    assert restored.rooms["z"]["r"].last_heated_at == analytics.rooms["z"]["r"].last_heated_at
    assert restored.get_analytics("z", "r", 18.5, 21.0, False).logged_stretches == 1


def test_older_storage_without_a_log_restores(analytics):
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, ctx=context())
    feed(analytics, now, 2, 18.5, False)
    stored = analytics.get_history_for_storage()
    for room in stored["rooms"]["z"].values():
        del room["log"], room["last_heated_at"]
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_history(stored)
    assert restored.get_log("z", "r") == []
    assert restored.rooms["z"]["r"].heating_stretches == 1


# ---------------------------------------------------------------------------
# In the running system
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=False)
def frozen(freezer):
    freezer.move_to(NOON)
    return freezer


def two_zones():
    def zone(name, boiler, rooms):
        return {"name": name, "schedule": ALL_DAY_19, "heat_source": boiler, "rooms": rooms}

    return make_config({
        "down": zone("Down", "switch.boiler_down", {
            "lounge": make_room("Lounge", ["climate.lounge_1", "climate.lounge_2"], ["sensor.lounge"]),
            "hall": make_room("Hall", ["climate.hall"], ["sensor.hall"]),
        }),
        "up": zone("Up", "switch.boiler_up", {
            "bedroom": make_room("Bedroom", ["climate.bedroom"], ["sensor.bedroom"]),
        }),
    })


async def setup_house(hass, add_trvs):
    await add_trvs(*(FakeTRV(name) for name in ("lounge_1", "lounge_2", "hall", "bedroom")))
    for sensor, value in (("lounge", 17.0), ("hall", 19.2), ("bedroom", 19.2)):
        set_temp(hass, f"sensor.{sensor}", value)


def report(hass):
    """Sensors report again (unchanged), so their readings stay fresh."""
    for sensor in ("lounge", "hall", "bedroom"):
        state = hass.states.get(f"sensor.{sensor}")
        hass.states.async_set(state.entity_id, state.state, state.attributes, force_update=True)


async def test_running_system_logs_cold_starts_and_joining(hass, add_trvs, make_coordinator, frozen):
    await setup_house(hass, add_trvs)
    hass.states.async_set("switch.boiler_down", "off")
    hass.states.async_set("switch.boiler_up", "off")
    coordinator = make_coordinator(two_zones())
    await coordinator.async_refresh()

    async def minutes(count, step=5):
        for _ in range(count // step):
            frozen.tick(timedelta(minutes=step))
            report(hass)
            await coordinator.async_refresh()

    # Downstairs boiler comes on for the lounge (2 TRVs): a cold start
    hass.states.async_set("switch.boiler_down", "on")
    await minutes(20)
    # The hall cools and joins heat that's been flowing for 20 minutes
    set_temp(hass, "sensor.hall", 17.5)
    await minutes(10)
    # Upstairs (its own heat source) starts with downstairs already hot
    set_temp(hass, "sensor.bedroom", 17.5)
    hass.states.async_set("switch.boiler_up", "on")
    await minutes(30)
    hass.states.async_set("switch.boiler_down", "off")
    hass.states.async_set("switch.boiler_up", "off")
    await minutes(5)

    analytics = coordinator.heating_analytics
    [lounge] = analytics.get_log("down", "lounge")
    [hall] = analytics.get_log("down", "hall")
    [bedroom] = analytics.get_log("up", "bedroom")
    assert (lounge["zone_heat_minutes"], lounge["house_heat_minutes"], lounge["trvs_heating"]) == (0.0, 0.0, 2)
    assert (hall["zone_heat_minutes"], hall["house_heat_minutes"], hall["trvs_heating"]) == (20.0, 20.0, 3)
    assert (bedroom["zone_heat_minutes"], bedroom["house_heat_minutes"], bedroom["trvs_heating"]) == (0.0, 30.0, 4)
    assert lounge["target"] == 19.0 and lounge["climb_needed"] == 2.0
    assert coordinator.data["down"]["rooms"]["lounge"]["heating_analytics"]["logged_stretches"] == 1


async def test_heat_already_flowing_at_startup_is_unknown(hass, add_trvs, make_coordinator, frozen):
    await setup_house(hass, add_trvs)
    hass.states.async_set("switch.boiler_down", "on")
    hass.states.async_set("switch.boiler_up", "off")
    coordinator = make_coordinator(two_zones())
    for _ in range(4):
        await coordinator.async_refresh()
        frozen.tick(timedelta(minutes=5))
        report(hass)
    hass.states.async_set("switch.boiler_down", "off")
    await coordinator.async_refresh()
    [lounge] = coordinator.heating_analytics.get_log("down", "lounge")
    assert lounge["zone_heat_minutes"] is None
    assert lounge["house_heat_minutes"] is None


async def test_diagnostics(hass, env):
    from custom_components.heating_manager.diagnostics import async_get_config_entry_diagnostics

    from .test_config_flow import base_options, entry_from_options, setup_entry

    entry = await setup_entry(hass, entry_from_options(base_options()))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    lounge = diagnostics["zones"]["downstairs"]["rooms"]["lounge"]
    assert lounge["heated_stretches"] == []
    assert lounge["trvs"] == 1
    assert "settings" in diagnostics
