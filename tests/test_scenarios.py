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
