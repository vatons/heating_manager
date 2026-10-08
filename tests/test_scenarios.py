"""End-to-end heating scenarios: schedule changes, TRV response and closed-loop warm-up.

These drive the real coordinator against fake TRVs. A TRV "opens" when its
setpoint is above its own internal temperature; real TRVs usually need a
margin, so the checks below require OPEN_MARGIN.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest

from .conftest import FakeTRV, local_dt, make_config, make_room, set_temp

TRV = "climate.room_trv"
SENSOR = "sensor.room_temperature"
OPEN_MARGIN = 0.5      # °C a TRV setpoint must exceed its internal reading to open reliably

# Weekday: 17.5°C until 16:00, then 20°C; the change at 16:00 is what the scenarios test
STEP_SCHEDULE = {
    "weekday": [
        {"start": "00:00", "end": "16:00", "temperature": 17.5},
        {"start": "16:00", "end": "00:00", "temperature": 20.0},
    ],
    "weekend": [{"start": "00:00", "end": "00:00", "temperature": 17.5}],
}


# Functions, not constants: HA's test time zone is only set once the hass fixture runs
def BEFORE():
    return local_dt(hour=15, minute=0)


def AT_CHANGE():
    return local_dt(hour=16, minute=0)


@dataclass
class Rig:
    hass: object
    coordinator: object
    trv: FakeTRV
    freezer: object
    bias: float
    room_temp: float = 0.0

    def set_room(self, temp: float, radiator_heat: float = 0.0) -> None:
        """Room sensor reads temp; the TRV's own sensor reads temp + bias + radiator heat."""
        self.room_temp = temp
        set_temp(self.hass, SENSOR, round(temp, 3))
        self.trv.set_internal_temperature(round(temp + self.bias + radiator_heat, 3))

    async def tick(self, minutes: int = 1) -> None:
        self.freezer.tick(timedelta(minutes=minutes))
        await self.coordinator.async_refresh()
        assert self.coordinator.last_update_success

    @property
    def room(self) -> dict:
        return self.coordinator.data["zone_1"]["rooms"]["room"]

    @property
    def demand(self) -> bool:
        return self.coordinator.data["zone_1"]["heating_demand"]

    @property
    def margin(self) -> float:
        return self.trv.target_temperature - self.trv.current_temperature


@pytest.fixture
def rig(hass, add_trvs, make_coordinator, freezer):
    async def _build(
        *,
        mode: str = "any_room",
        bias: float = 0.0,
        max_temp: float = 30.0,
        step: float | None = None,
        start_temp: float = 17.5,
        settle_minutes: int = 60,
    ) -> Rig:
        freezer.move_to(BEFORE() - timedelta(minutes=settle_minutes))
        trv = (await add_trvs(FakeTRV("room_trv", max_temp=max_temp, target_temperature_step=step)))[TRV]
        coordinator = make_coordinator(make_config(
            {"zone_1": {"schedule": STEP_SCHEDULE, "rooms": {
                "room": make_room("Room", [TRV], [SENSOR]),
            }}},
            heating_demand_mode=mode,
        ))
        r = Rig(hass, coordinator, trv, freezer, bias)
        # Settle at the old 17.5°C target with a cold radiator, so the TRV offset is learned
        r.set_room(start_temp)
        await coordinator.async_refresh()
        for i in range(settle_minutes):
            r.set_room(start_temp + (i % 2) * 0.01)
            await r.tick()
        return r

    return _build


# ---------------------------------------------------------------------------
# Schedule step 17.5 -> 20°C: does the room call for heat and the TRV open?
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["any_room", "zone_average"])
@pytest.mark.parametrize("bias", [0.0, 2.0, -1.0], ids=["trv_accurate", "trv_reads_warm", "trv_reads_cold"])
@pytest.mark.parametrize("room_temp", [19.5, 18.0, 15.0])
async def test_schedule_increase_opens_trv_and_fires_boiler(rig, mode, bias, room_temp):
    r = await rig(mode=mode, bias=bias, start_temp=room_temp)
    assert r.room["target_temperature"] == 17.5

    r.freezer.move_to(AT_CHANGE())
    r.set_room(room_temp)
    await r.tick(0)

    assert r.room["target_temperature"] == 20.0
    assert r.room["needs_heating"] is True
    assert r.demand is True, "boiler must fire"
    assert r.margin >= OPEN_MARGIN, (
        f"TRV setpoint {r.trv.target_temperature} vs internal {r.trv.current_temperature}"
    )


@pytest.mark.parametrize("mode", ["any_room", "zone_average"])
async def test_small_increase_still_heats(rig, mode):
    """Room 0.2°C under the new target: it should still heat up to target."""
    r = await rig(mode=mode, start_temp=19.8)
    r.freezer.move_to(AT_CHANGE())
    r.set_room(19.8)
    await r.tick(0)
    assert r.room["needs_heating"] is True
    assert r.margin >= OPEN_MARGIN
    assert r.demand is True, "boiler must fire, or the room stays 0.2°C short indefinitely"


async def test_cold_room_with_limited_trv_opens_fully(rig):
    """15°C room, TRV reads 2°C warm and tops out at 25°C: setpoint pinned at max, valve open."""
    r = await rig(bias=2.0, max_temp=25.0, start_temp=15.0)
    r.freezer.move_to(AT_CHANGE())
    r.set_room(15.0)
    await r.tick(0)
    assert r.trv.target_temperature == 25.0
    assert r.margin >= OPEN_MARGIN


async def test_half_degree_trv_still_opens_for_small_deficit(rig):
    r = await rig(step=0.5, bias=0.3, start_temp=19.5)
    r.freezer.move_to(AT_CHANGE())
    r.set_room(19.5)
    await r.tick(0)
    assert r.trv.target_temperature * 2 == round(r.trv.target_temperature * 2)
    assert r.margin >= OPEN_MARGIN


# ---------------------------------------------------------------------------
# Schedule decrease and rooms at or above target
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bias", [0.0, 2.0])
async def test_schedule_decrease_closes_trv(rig, bias):
    """20 -> 17.5°C with the room at 20: TRV shuts, boiler stops."""
    r = await rig(bias=bias, start_temp=20.0, settle_minutes=30)
    r.freezer.move_to(local_dt(hour=16, minute=30))   # in the 20°C period
    r.set_room(20.0)
    await r.tick(0)
    r.freezer.move_to(local_dt(day=15, hour=0, minute=1))   # next day 00:01: 17.5°C
    r.set_room(20.0)
    await r.tick(0)
    assert r.room["target_temperature"] == 17.5
    assert r.room["needs_heating"] is False
    assert r.demand is False
    assert r.margin < 0, "TRV must close"


async def test_room_at_target_trv_holds_without_calling_boiler(rig):
    r = await rig(start_temp=17.5)
    assert r.room["needs_heating"] is False
    assert r.demand is False


# ---------------------------------------------------------------------------
# Closed loop: a simple room/radiator model driven minute by minute
# ---------------------------------------------------------------------------

@dataclass
class Thermal:
    """Radiator heats only when the TRV is open AND the boiler is firing."""

    room: float
    outside: float = 5.0
    radiator: float = 0.0
    coupling: float = 0.08     # how much radiator heat the TRV's own sensor picks up

    def step(self, valve_open: bool, boiler_on: bool) -> None:
        if not self.radiator:
            self.radiator = self.room
        if valve_open and boiler_on:
            self.radiator += (65.0 - self.radiator) * 0.08
        else:
            self.radiator += (self.room - self.radiator) * 0.03
        self.room += 0.0012 * (self.radiator - self.room) - 0.0025 * (self.room - self.outside)

    @property
    def trv_heat(self) -> float:
        return self.coupling * (self.radiator - self.room)


async def run_closed_loop(r: Rig, model: Thermal, minutes: int):
    history = []
    for _ in range(minutes):
        model.step(r.trv.valve_open, r.demand)
        r.set_room(model.room, radiator_heat=model.trv_heat)
        await r.tick()
        history.append((model.room, r.trv.valve_open, r.demand))
    return history


@pytest.mark.parametrize("mode", ["any_room", "zone_average"])
@pytest.mark.parametrize(
    ("start", "coupling"),
    [(15.0, 0.08), (19.5, 0.08), (15.0, 0.2), (19.5, 0.2)],
    ids=["cold_room", "nearly_warm", "cold_room_trv_by_radiator", "nearly_warm_trv_by_radiator"],
)
async def test_closed_loop_reaches_target_without_overshoot(rig, mode, start, coupling):
    r = await rig(mode=mode, start_temp=start, settle_minutes=30)
    r.freezer.move_to(AT_CHANGE())
    model = Thermal(room=start, coupling=coupling)
    history = await run_closed_loop(r, model, minutes=6 * 60)

    temps = [h[0] for h in history]
    reached = next((i for i, t in enumerate(temps) if t >= 19.9), None)
    assert reached is not None, f"never reached 19.9°C; max {max(temps):.2f}"
    after = temps[reached:]
    assert max(after) <= 20.6, f"overshoot to {max(after):.2f}"
    # Once warm, it should stay within the deadband (allowing for the 0.3°C deadband + slack)
    assert min(after[60:]) >= 19.5, f"dropped to {min(after[60:]):.2f} after reaching target"


# ---------------------------------------------------------------------------
# Shared builder for multi-room scenarios
# ---------------------------------------------------------------------------

ALL_DAY_20 = {
    "weekday": [{"start": "00:00", "end": "00:00", "temperature": 20.0}],
    "weekend": [{"start": "00:00", "end": "00:00", "temperature": 20.0}],
}


@pytest.fixture
def zone(hass, add_trvs, make_coordinator, freezer):
    """Build zone_1 from {room_id: (has_trv, initial_temp)}; returns (coordinator, trvs, set_room, tick)."""

    async def _build(rooms: dict, *, mode="any_room", schedule=ALL_DAY_20, start=None, **coordinator_kwargs):
        freezer.move_to(start or local_dt(hour=12))
        trvs = await add_trvs(*[
            FakeTRV(f"{rid}_trv", current_temperature=temp)
            for rid, (has_trv, temp) in rooms.items() if has_trv
        ])
        coordinator = make_coordinator(make_config(
            {"zone_1": {"schedule": schedule, "rooms": {
                rid: make_room(rid, [f"climate.{rid}_trv"] if has_trv else [], [f"sensor.{rid}"])
                for rid, (has_trv, _) in rooms.items()
            }}},
            heating_demand_mode=mode,
        ), **coordinator_kwargs)

        def set_room(rid, temp, trv_temp=None):
            set_temp(hass, f"sensor.{rid}", round(temp, 3))
            if f"climate.{rid}_trv" in trvs:
                trvs[f"climate.{rid}_trv"].set_internal_temperature(
                    round(temp if trv_temp is None else trv_temp, 3)
                )

        async def tick(minutes=1):
            freezer.tick(timedelta(minutes=minutes))
            await coordinator.async_refresh()
            assert coordinator.last_update_success

        for rid, (_, temp) in rooms.items():
            set_room(rid, temp)
        await coordinator.async_refresh()
        return coordinator, trvs, set_room, tick

    return _build


def room_of(coordinator, rid):
    return coordinator.data["zone_1"]["rooms"][rid]


def demand_of(coordinator):
    return coordinator.data["zone_1"]["heating_demand"]


def margin_of(trv):
    return trv.target_temperature - trv.current_temperature


# ---------------------------------------------------------------------------
# Room sensor drops out while heating
# ---------------------------------------------------------------------------

async def test_sensor_dropout_uses_last_value_then_zone_average(hass, zone):
    coordinator, trvs, set_room, tick = await zone({"lounge": (True, 18.0), "hall": (False, 19.0)})
    lounge_trv = trvs["climate.lounge_trv"]
    assert room_of(coordinator, "lounge")["needs_heating"] and demand_of(coordinator)
    learned = coordinator.trv_controller._get_ema_offset("zone_1", "lounge", "climate.lounge_trv")

    hass.states.async_set("sensor.lounge", "unavailable")
    for minute in range(1, 41):
        set_room("hall", 19.0 + (minute % 2) * 0.01)
        await tick()
        lounge = room_of(coordinator, "lounge")
        if minute < 30:
            # Recent last-known reading keeps the room heating normally
            assert (lounge["temperature"], lounge["temperature_source"]) == (18.0, "local_sensors")
        assert lounge["needs_heating"] is True
        assert demand_of(coordinator) is True
        assert margin_of(lounge_trv) >= OPEN_MARGIN
    # After the 30-minute timeout, the zone's other sensors stand in
    assert room_of(coordinator, "lounge")["temperature_source"] == "zone_average"
    assert room_of(coordinator, "lounge")["temperature"] == pytest.approx(19.0, abs=0.02)
    # ...without corrupting the TRV's learned offset
    assert coordinator.trv_controller._get_ema_offset(
        "zone_1", "lounge", "climate.lounge_trv"
    ) == pytest.approx(learned)

    set_room("lounge", 18.4)
    await tick()
    assert room_of(coordinator, "lounge")["temperature_source"] == "local_sensors"
    assert room_of(coordinator, "lounge")["temperature"] == 18.4


async def test_sensor_dropout_with_no_fallback_stops_calling_for_heat(hass, zone):
    """Only sensor in the zone: once it's 30 min stale the room stops demanding heat."""
    coordinator, trvs, set_room, tick = await zone({"lounge": (True, 18.0)})
    hass.states.async_set("sensor.lounge", "unavailable")
    await tick(31)
    lounge = room_of(coordinator, "lounge")
    assert lounge["temperature"] is None
    assert lounge["needs_heating"] is False
    assert demand_of(coordinator) is False


async def test_sensor_dropout_with_trv_fallback_keeps_heating(hass, zone):
    coordinator, trvs, set_room, tick = await zone({"lounge": (True, 18.0)}, fallback_mode="trv")
    hass.states.async_set("sensor.lounge", "unavailable")
    await tick(31)
    lounge = room_of(coordinator, "lounge")
    assert (lounge["temperature"], lounge["temperature_source"]) == (18.0, "trv")
    assert lounge["needs_heating"] is True
    assert demand_of(coordinator) is True


# ---------------------------------------------------------------------------
# Boost expires while heating
# ---------------------------------------------------------------------------

async def test_boost_expiry_returns_to_schedule(hass, zone):
    coordinator, trvs, set_room, tick = await zone({"lounge": (True, 20.0)})
    trv = trvs["climate.lounge_trv"]
    assert demand_of(coordinator) is False

    await coordinator.set_boost("zone_1", "lounge", duration=30, temperature=23.0)
    await tick(0)
    assert room_of(coordinator, "lounge")["target_temperature"] == 23.0
    assert demand_of(coordinator) is True
    assert margin_of(trv) >= OPEN_MARGIN

    # The boost warms the room to 21.5°C over its 30 minutes
    for minute in range(30):
        set_room("lounge", 20.0 + 1.5 * (minute + 1) / 30)
        await tick()
    assert room_of(coordinator, "lounge")["boost"] is not None
    await tick(1)

    lounge = room_of(coordinator, "lounge")
    assert lounge["boost"] is None
    assert lounge["target_temperature"] == 20.0
    assert lounge["needs_heating"] is False
    assert demand_of(coordinator) is False
    assert margin_of(trv) < 0, "room is 1.5°C over target: TRV must close"
    assert coordinator.boost_manager.boost_state == {}


async def test_boost_expiry_while_still_cold_keeps_heating_to_schedule(hass, zone):
    coordinator, trvs, set_room, tick = await zone({"lounge": (True, 16.0)})
    await coordinator.set_boost("zone_1", "lounge", duration=30, temperature=23.0)
    for minute in range(31):                  # room warms slowly but stays cold
        set_room("lounge", 16.0 + minute * 0.02)
        await tick()
    lounge = room_of(coordinator, "lounge")
    assert lounge["boost"] is None
    assert lounge["target_temperature"] == 20.0
    assert lounge["needs_heating"] is True
    assert demand_of(coordinator) is True
    assert margin_of(trvs["climate.lounge_trv"]) >= OPEN_MARGIN


# ---------------------------------------------------------------------------
# Clock changes (UK): schedules follow local time; boosts follow real time
# ---------------------------------------------------------------------------

DST_SCHEDULE = {
    "weekday": [{"start": "06:00", "end": "22:00", "temperature": 20.0}],
    "weekend": [
        {"start": "01:00", "end": "02:00", "temperature": 18.0},
        {"start": "06:00", "end": "22:00", "temperature": 20.0},
    ],
}


def utc(*args):
    from datetime import datetime, timezone
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("when_utc", "local", "expected"),
    [
        # Spring forward, Sunday 29 March 2026: 01:00 GMT -> 02:00 BST
        (utc(2026, 3, 29, 0, 30), "00:30 GMT", 10.0),
        (utc(2026, 3, 29, 4, 59), "05:59 BST", 10.0),
        (utc(2026, 3, 29, 5, 0), "06:00 BST", 20.0),
        (utc(2026, 3, 29, 20, 59), "21:59 BST", 20.0),
        (utc(2026, 3, 29, 21, 0), "22:00 BST", 10.0),
        # Fall back, Sunday 25 October 2026: 02:00 BST -> 01:00 GMT (01:xx happens twice)
        (utc(2026, 10, 25, 0, 30), "01:30 BST", 18.0),
        (utc(2026, 10, 25, 1, 30), "01:30 GMT", 18.0),
        (utc(2026, 10, 25, 2, 0), "02:00 GMT", 10.0),
        (utc(2026, 10, 25, 5, 59), "05:59 GMT", 10.0),
        (utc(2026, 10, 25, 6, 0), "06:00 GMT", 20.0),
    ],
)
async def test_schedule_follows_local_time_across_clock_changes(hass, zone, when_utc, local, expected):
    await hass.config.async_set_time_zone("Europe/London")
    coordinator, trvs, set_room, tick = await zone(
        {"lounge": (True, 17.0)}, schedule=DST_SCHEDULE, start=when_utc,
    )
    assert room_of(coordinator, "lounge")["target_temperature"] == expected, local


async def test_boost_lasts_real_minutes_across_clock_change(hass, zone, freezer):
    await hass.config.async_set_time_zone("Europe/London")
    # 00:30 GMT on the spring-forward night; a 2-hour boost ends at 03:30 BST (02:30 UTC)
    coordinator, trvs, set_room, tick = await zone(
        {"lounge": (True, 17.0)}, schedule=DST_SCHEDULE, start=utc(2026, 3, 29, 0, 30),
    )
    await coordinator.set_boost("zone_1", "lounge", duration=120, temperature=22.0)
    freezer.move_to(utc(2026, 3, 29, 2, 29))
    await coordinator.async_refresh()
    assert room_of(coordinator, "lounge")["target_temperature"] == 22.0
    freezer.move_to(utc(2026, 3, 29, 2, 31))
    await coordinator.async_refresh()
    assert room_of(coordinator, "lounge")["boost"] is None


async def test_boost_lasts_real_minutes_when_clocks_go_back(hass, zone, freezer):
    await hass.config.async_set_time_zone("Europe/London")
    # 00:30 BST on the fall-back night; a 2-hour boost ends at 01:30 GMT (01:30 UTC)
    coordinator, trvs, set_room, tick = await zone(
        {"lounge": (True, 17.0)}, schedule=DST_SCHEDULE, start=utc(2026, 10, 24, 23, 30),
    )
    await coordinator.set_boost("zone_1", "lounge", duration=120, temperature=22.0)
    freezer.move_to(utc(2026, 10, 25, 1, 29))
    await coordinator.async_refresh()
    assert room_of(coordinator, "lounge")["target_temperature"] == 22.0
    freezer.move_to(utc(2026, 10, 25, 1, 31))
    await coordinator.async_refresh()
    assert room_of(coordinator, "lounge")["boost"] is None, "boost ran an hour long"


async def test_boost_time_remaining_shown_correctly_across_clock_change(hass, zone, freezer):
    await hass.config.async_set_time_zone("Europe/London")
    coordinator, trvs, set_room, tick = await zone(
        {"lounge": (True, 17.0)}, schedule=DST_SCHEDULE, start=utc(2026, 3, 29, 0, 30),
    )
    await coordinator.set_boost("zone_1", "lounge", duration=120, temperature=22.0)
    from custom_components.heating_manager.climate import RoomClimate
    entity = RoomClimate(coordinator, "zone_1", "lounge", {"name": "lounge"})
    freezer.move_to(utc(2026, 3, 29, 1, 30))      # 02:30 BST: one real hour left
    boost = coordinator.boost_manager.get_boost_info("zone_1", "lounge", utc(2026, 3, 29, 1, 30))
    assert entity._calculate_time_remaining(boost) == 60


async def test_watchdog_measures_real_time_across_clock_change(hass, zone, freezer):
    """The 240-minute watchdog must not fire an hour early when clocks go forward."""
    await hass.config.async_set_time_zone("Europe/London")
    rooms = {"lounge": (True, 15.0)}
    coordinator, trvs, set_room, tick = await zone(
        rooms, schedule=ALL_DAY_20, start=utc(2026, 3, 28, 22, 0),
    )
    trv = trvs["climate.lounge_trv"]
    # 3h30 of real time spanning 01:00 GMT -> 02:00 BST: below the 4h limit
    for minute in range(210):
        set_room("lounge", 15.0 + (minute % 2) * 0.01)
        await tick()
    assert coordinator.minimum_temp not in trv.set_temperature_calls, "watchdog fired early"
    assert coordinator.data["zone_1"]["heating_demand"] is True


async def test_heating_rate_not_distorted_by_clock_change(hass, zone, freezer):
    await hass.config.async_set_time_zone("Europe/London")
    coordinator, trvs, set_room, tick = await zone(
        {"lounge": (True, 18.0)}, schedule=ALL_DAY_20, start=utc(2026, 10, 25, 0, 30),
    )
    # Warm at 1.2°C/hour (0.02/min) through the 02:00 BST -> 01:00 GMT change
    for minute in range(1, 121):
        set_room("lounge", 18.0 + 0.02 * minute)
        await tick()
    rate = room_of(coordinator, "lounge")["heating_analytics"]["heating_rate"]
    assert rate == pytest.approx(1.2, abs=0.15)


async def test_manual_override_expires_at_period_change_after_clock_change(hass, zone, freezer):
    await hass.config.async_set_time_zone("Europe/London")
    coordinator, trvs, set_room, tick = await zone(
        {"lounge": (True, 17.0)}, schedule=DST_SCHEDULE, start=utc(2026, 10, 25, 2, 30),
    )
    await coordinator.set_manual_zone_temperature("zone_1", 15.0)    # 02:30 GMT, 10°C period
    freezer.move_to(utc(2026, 10, 25, 5, 59))
    await coordinator.async_refresh()
    assert room_of(coordinator, "lounge")["target_temperature"] == 15.0
    freezer.move_to(utc(2026, 10, 25, 6, 0))                          # 06:00 GMT: 20°C period
    await coordinator.async_refresh()
    assert room_of(coordinator, "lounge")["target_temperature"] == 20.0
    await coordinator.async_refresh()
    assert coordinator.manual_zone_temp == {}


# ---------------------------------------------------------------------------
# TRV goes unavailable (flat battery, Zigbee dropout) and comes back
# ---------------------------------------------------------------------------

async def test_trv_unavailable_then_returns(hass, zone):
    coordinator, trvs, set_room, tick = await zone({"lounge": (True, 17.0)})
    trv = trvs["climate.lounge_trv"]
    for i in range(10):   # learn an offset: TRV reads 1°C warm
        set_room("lounge", 17.0 + (i % 2) * 0.01, trv_temp=18.0)
        await tick()
    learned = coordinator.trv_controller._get_ema_offset("zone_1", "lounge", "climate.lounge_trv")

    trv.set_available(False)
    for _ in range(5):
        await tick()
        assert hass.states.get("climate.lounge_trv").state == "unavailable"
        # The rest of the system carries on: room still needs heat, boiler still called
        assert room_of(coordinator, "lounge")["needs_heating"] is True
        assert demand_of(coordinator) is True
    assert coordinator.trv_controller._get_ema_offset(
        "zone_1", "lounge", "climate.lounge_trv"
    ) == pytest.approx(learned)

    # Comes back after a battery change: off, with a low setpoint
    trv._attr_hvac_mode = "off"
    trv._attr_target_temperature = 7.0
    trv.set_available(True)
    await tick()
    assert trv.hvac_mode == "heat"
    assert margin_of(trv) >= OPEN_MARGIN


async def test_other_rooms_unaffected_by_unavailable_trv(hass, zone):
    coordinator, trvs, set_room, tick = await zone({"lounge": (True, 17.0), "study": (True, 17.0)})
    trvs["climate.lounge_trv"].set_available(False)
    trvs["climate.study_trv"].set_internal_temperature(17.0)
    trvs["climate.study_trv"]._attr_target_temperature = 7.0
    await tick()
    assert margin_of(trvs["climate.study_trv"]) >= OPEN_MARGIN


# ---------------------------------------------------------------------------
# zone_average with several rooms: one cold room among warm ones
# ---------------------------------------------------------------------------

async def settle_at_target(set_room, tick, rooms):
    for i in range(5):
        for rid in rooms:
            set_room(rid, 20.0 + (i % 2) * 0.01)
        await tick()


async def test_zone_average_cold_room_is_outvoted(hass, zone):
    """Documents zone_average: a cold room among warm rooms doesn't fire the boiler.

    Its TRV still opens, so it warms whenever the boiler runs for the zone.
    Use any_room mode if every room must get heat.
    """
    rooms = {"bedroom": (True, 20.0), "lounge": (True, 20.0), "study": (True, 20.0)}
    coordinator, trvs, set_room, tick = await zone(rooms, mode="zone_average")
    await settle_at_target(set_room, tick, rooms)

    set_room("bedroom", 15.5)
    set_room("lounge", 22.0)
    set_room("study", 22.0)
    await tick(10)          # 10 minutes: within the plausibility limit for a 4.5°C change
    assert room_of(coordinator, "bedroom")["needs_heating"] is True
    assert margin_of(trvs["climate.bedroom_trv"]) >= OPEN_MARGIN
    assert margin_of(trvs["climate.lounge_trv"]) < 0
    assert demand_of(coordinator) is False                 # average 19.83°C: within deadband


async def test_any_room_fires_for_one_cold_room(hass, zone):
    rooms = {"bedroom": (True, 20.0), "lounge": (True, 20.0), "study": (True, 20.0)}
    coordinator, trvs, set_room, tick = await zone(rooms, mode="any_room")
    await settle_at_target(set_room, tick, rooms)
    set_room("bedroom", 15.5)
    set_room("lounge", 22.0)
    set_room("study", 22.0)
    await tick(10)
    assert demand_of(coordinator) is True


async def test_zone_average_fires_when_average_drops(hass, zone):
    rooms = {"bedroom": (True, 20.0), "lounge": (True, 20.0), "study": (True, 20.0)}
    coordinator, trvs, set_room, tick = await zone(rooms, mode="zone_average")
    await settle_at_target(set_room, tick, rooms)
    set_room("bedroom", 18.5)
    set_room("lounge", 19.8)
    set_room("study", 19.6)
    await tick(10)
    assert demand_of(coordinator) is True                  # average 19.3°C
    assert margin_of(trvs["climate.bedroom_trv"]) >= OPEN_MARGIN
