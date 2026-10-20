"""The log of heated stretches: what each started from (a cold or already hot system, how
many TRVs shared the heat, how long since the room was last heated) and what it showed."""
from __future__ import annotations

from datetime import timedelta
import json

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


def restored_from(analytics, keep=None):
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_history(analytics.get_history_for_storage())
    restored.restore_log(json.loads(json.dumps(analytics.get_log_for_storage())), keep)
    return restored


def test_log_survives_a_restart(analytics):
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, outdoor=4.5, ctx=context())
    now = feed(analytics, now, 20, 18.5, False)
    feed(analytics, now, 30, 18.2, True, rate=1.0, ctx=context(zone_heat_minutes=None, house_heat_minutes=None))
    feed(analytics, now + 30 * MINUTE, 2, 18.7, False)
    restored = restored_from(analytics)
    assert restored.get_log("z", "r") == analytics.get_log("z", "r")     # every field and type
    assert restored.get_log("z", "r")[1]["zone_heat_minutes"] is None
    assert restored.rooms["z"]["r"].last_heated_at == analytics.rooms["z"]["r"].last_heated_at
    assert restored.get_analytics("z", "r", 18.5, 21.0, False).logged_stretches == 2


def test_learned_figures_dont_carry_the_log(analytics):
    """The log has its own file: the state saved every few minutes stays small."""
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, ctx=context())
    feed(analytics, now, 2, 18.5, False)
    assert "log" not in json.dumps(analytics.get_history_for_storage())


def test_storage_from_before_the_log_restores(analytics):
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, ctx=context())
    feed(analytics, now, 2, 18.5, False)
    stored = analytics.get_history_for_storage()
    for room in stored["rooms"]["z"].values():
        del room["last_heated_at"]
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_history(stored)
    restored.restore_log(None)
    assert restored.get_log("z", "r") == []
    assert restored.rooms["z"]["r"].heating_stretches == 1
    assert restored.rooms["z"]["r"].last_heated_at is None


def test_full_logs_stay_small_on_disk():
    """Worst case: every room's log full, every field filled, as Home Assistant writes it
    (indented JSON). Under 28 KB a room."""
    from homeassistant.helpers.json import prepare_save_json

    analytics = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    entry = {
        "start": "2026-01-14T06:05:00.123456+00:00", "minutes": 179.9, "continued": False,
        "start_temp": 16.37, "end_temp": 21.41, "target": 21.5, "climb_needed": 5.13,
        "outdoor": -12.3, "warm_up_minutes": 123.4, "rate": 1.234, "zone_heat_minutes": 1234.5,
        "house_heat_minutes": 1234.5, "trvs_heating": 24, "trvs_heating_mean": 23.9,
        "minutes_since_heated": 12345.6, "overrun_rise": 1.23, "overrun_minutes": 123.4,
    }
    for zone in range(3):
        for room in range(5):
            analytics._room(f"zone_{zone}", f"room_{room}").log = [dict(entry)] * LOG_SIZE
    _, data = prepare_save_json(analytics.get_log_for_storage())
    assert len(data) < 15 * 28_000          # 15 rooms
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_log(json.loads(data))
    assert restored.get_log("zone_2", "room_4")[-1] == entry


def test_restore_skips_what_it_cant_read(analytics):
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, ctx=context())
    feed(analytics, now, 2, 18.5, False)
    stored = analytics.get_log_for_storage()
    good = stored["rooms"]["z"]["r"][0]
    stored["rooms"]["z"]["r"] = [
        good,
        "not,enough,values",
        good.replace("2026", "xxxx", 1),                     # bad start
        good.split(",", 1)[0] + ",abc" + good[good.index(",", good.index(",") + 1):],  # bad number
        42,
        good,
    ]
    stored["rooms"]["z"]["other"] = "not a list"
    stored["rooms"]["broken"] = None
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_log(stored)
    assert len(restored.get_log("z", "r")) == 2
    for bad in (None, [], {"format": 99}, {"format": 1, "columns": None}):
        HeatingAnalytics(min_stretches=1, learning_rate=0.3).restore_log(bad)


def test_restore_matches_columns_by_name(analytics):
    """A log saved with columns in another order, an unknown one, or one missing, still loads."""
    stored = {
        "format": 1,
        "columns": ["future_field", "rate", "start", "trvs_heating"],
        "rooms": {"z": {"r": ["x,1.5,2026-01-14T06:00:00+00:00,3"]}},
    }
    analytics.restore_log(stored)
    [entry] = analytics.get_log("z", "r")
    assert (entry["rate"], entry["start"], entry["trvs_heating"]) == (1.5, "2026-01-14T06:00:00+00:00", 3)
    assert entry["outdoor"] is None and "future_field" not in entry


def test_restore_drops_deleted_rooms(analytics):
    for room_id in ("r", "gone"):
        now = T0
        for minute in range(32):
            analytics.record_temperature(
                "z", room_id, 18.0 + minute / 60, minute < 30, now + minute * MINUTE, context=context(),
            )
    restored = restored_from(analytics, keep={"z": {"r"}})
    assert len(restored.get_log("z", "r")) == 1
    assert restored.get_log("z", "gone") == []


def test_log_version_changes_only_when_a_stretch_is_logged(analytics):
    now = feed(analytics, T0, 30, 18.0, True, rate=1.0, ctx=context())
    assert analytics.log_version == 0                    # stretch still going
    now = feed(analytics, now, 30, 18.5, False)
    assert analytics.log_version == 1                    # heated stretch ended
    now = feed(analytics, now, 30, 18.0, False)
    assert analytics.log_version == 2                    # overrun over: added to its entry
    feed(analytics, now, 30, 17.8, False)
    assert analytics.log_version == 2                    # nothing new to log


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


# ---------------------------------------------------------------------------
# Saving: its own file, written only after a stretch is logged
# ---------------------------------------------------------------------------

async def heat_lounge_once(hass, coordinator, frozen, minutes=30):
    """The lounge (17 °C) gets heat for a while, then the boiler stops."""
    hass.states.async_set("switch.boiler_down", "on")
    for _ in range(minutes // 5):
        frozen.tick(timedelta(minutes=5))
        report(hass)
        await coordinator.async_refresh()
    hass.states.async_set("switch.boiler_down", "off")
    frozen.tick(timedelta(minutes=5))
    report(hass)
    await coordinator.async_refresh()


async def wait(hass, frozen, seconds):
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    frozen.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_log_file_written_only_after_a_stretch_is_logged(
    hass, hass_storage, add_trvs, make_coordinator, frozen
):
    from custom_components.heating_manager.const import STORAGE_KEY
    from custom_components.heating_manager.coordinator import LOG_SAVE_DELAY, LOG_STORAGE_KEY

    await setup_house(hass, add_trvs)
    hass.states.async_set("switch.boiler_down", "off")
    hass.states.async_set("switch.boiler_up", "off")
    coordinator = make_coordinator(two_zones())
    await coordinator.async_refresh()

    # Updates without a stretch ending don't write the log
    for _ in range(6):
        frozen.tick(timedelta(minutes=5))
        report(hass)
        await coordinator.async_refresh()
    await wait(hass, frozen, LOG_SAVE_DELAY + 1)
    assert LOG_STORAGE_KEY not in hass_storage

    # A stretch logged: written once the delay has passed, not straight away
    await heat_lounge_once(hass, coordinator, frozen)
    await wait(hass, frozen, 60)
    assert LOG_STORAGE_KEY not in hass_storage
    await wait(hass, frozen, LOG_SAVE_DELAY)
    saved = hass_storage[LOG_STORAGE_KEY]["data"]
    assert len(saved["rooms"]["down"]["lounge"]) == 1
    # The state saved every few minutes doesn't carry it
    await wait(hass, frozen, 600)
    assert "lounge" in json.dumps(hass_storage[STORAGE_KEY]["data"]["analytics_history"])
    assert '"log"' not in json.dumps(hass_storage[STORAGE_KEY])

    # Nothing new: not written again
    del hass_storage[LOG_STORAGE_KEY]
    for _ in range(6):
        frozen.tick(timedelta(minutes=5))
        report(hass)
        await coordinator.async_refresh()
    await wait(hass, frozen, LOG_SAVE_DELAY + 1)
    assert LOG_STORAGE_KEY not in hass_storage

    # Saving on unload writes a pending log straight away
    await heat_lounge_once(hass, coordinator, frozen)
    await coordinator.async_save_state()
    assert len(hass_storage[LOG_STORAGE_KEY]["data"]["rooms"]["down"]["lounge"]) == 2


async def test_log_loaded_after_a_restart_without_deleted_rooms(
    hass, hass_storage, add_trvs, make_coordinator, frozen
):
    from custom_components.heating_manager.coordinator import LOG_STORAGE_KEY

    await setup_house(hass, add_trvs)
    hass.states.async_set("switch.boiler_down", "off")
    hass.states.async_set("switch.boiler_up", "off")
    coordinator = make_coordinator(two_zones())
    await coordinator.async_refresh()
    await heat_lounge_once(hass, coordinator, frozen)
    await coordinator.async_save_state()
    hass_storage[LOG_STORAGE_KEY]["data"]["rooms"]["down"]["deleted_room"] = (
        hass_storage[LOG_STORAGE_KEY]["data"]["rooms"]["down"]["lounge"]
    )

    after = make_coordinator(two_zones())
    await after.async_refresh()               # loads saved state first
    assert len(after.heating_analytics.get_log("down", "lounge")) == 1
    assert after.heating_analytics.get_log("down", "deleted_room") == []
