"""The overrun: after heating stops, radiators keep warming the room for a while. It is
learned per room (how far, how long to the peak), cooling is learned from the peak on,
and the time-to-target estimate says when the room is still warming, warming from
something else, or won't reach its target before the target changes."""
from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.heating_manager.heating_analytics import HeatingAnalytics

from .conftest import FakeTRV, local_dt, make_config, make_room, set_temp
from .test_heating_analytics import MINUTE, Room


@pytest.fixture
def analytics():
    return HeatingAnalytics(min_stretches=1, learning_rate=0.3)


def heat_then_overrun(room: Room, rise_minutes=40, rise_rate=0.6, cool_rate=-0.3, cool_minutes=60):
    """40 minutes' heating, then still rising without heat, then cooling; heat coming
    back on ends the stretch, so its cooling is learned."""
    room.run(40, rate=1.5)
    room.run(rise_minutes, rate=rise_rate, heated=False)
    room.run(cool_minutes, rate=cool_rate, heated=False)
    room.run(1, rate=1.5)
    return room


def settled_after_heat(room: Room):
    """A learned cycle, then heat, its overrun, and cooling under way."""
    heat_then_overrun(room)
    room.run(40, rate=1.5)
    room.run(40, rate=0.6, heated=False)
    room.run(25, rate=-0.3, heated=False)
    return room


def learned(analytics):
    return analytics.rooms["z"]["r"]


# ---------------------------------------------------------------------------
# Learning the overrun
# ---------------------------------------------------------------------------

def test_overrun_learned_and_cooling_from_the_peak(analytics):
    heat_then_overrun(Room(analytics))
    room = learned(analytics)
    assert room.overrun_stretches == 1
    assert room.overrun_rise == pytest.approx(0.4, abs=0.02)        # 40 min at 0.6 °C/h
    assert room.overrun_minutes == pytest.approx(39, abs=2)
    # Cooling learned from the peak only: not pulled up by the rise
    assert room.cooling_rate == pytest.approx(-0.3, abs=0.02)


def test_overrun_on_a_plateau_ends_after_no_new_high(analytics):
    room = Room(analytics)
    room.run(40, rate=1.5)
    room.run(20, rate=0.6, heated=False)                     # rises 0.2
    room.run(29, rate=0.0, heated=False)                     # then holds
    assert learned(analytics).overrun_stretches == 0
    room.run(3, rate=0.0, heated=False)                      # 30 min without a new high
    assert learned(analytics).overrun_stretches == 1
    assert learned(analytics).overrun_rise == pytest.approx(0.2, abs=0.02)


def test_no_overrun_when_the_room_cools_straight_away(analytics):
    room = Room(analytics)
    room.run(40, rate=1.5)
    room.run(60, rate=-0.5, heated=False)
    room.run(1, rate=1.5)
    assert learned(analytics).overrun_rise == pytest.approx(0.0)
    assert learned(analytics).overrun_minutes == pytest.approx(0.0)
    assert learned(analytics).cooling_rate == pytest.approx(-0.5, abs=0.02)


def test_not_over_before_cool_settle(analytics):
    """A sensor can lag: a dip in the first minutes doesn't end the overrun."""
    room = Room(analytics)
    room.run(40, rate=1.5)
    room.run(5, rate=-1.5, heated=False)                     # dips 0.12
    room.run(30, rate=1.0, heated=False)                     # then rises 0.5
    room.run(30, rate=-0.3, heated=False)
    assert learned(analytics).overrun_rise == pytest.approx(0.38, abs=0.03)


def test_heat_returning_during_the_overrun_learns_nothing(analytics):
    room = Room(analytics)
    room.run(40, rate=1.5)
    room.run(10, rate=0.6, heated=False)
    room.run(20, rate=1.5)
    assert learned(analytics).overrun_stretches == 0


def test_after_a_restart_no_overrun_is_learned(analytics):
    """The heating before a restart isn't known: wait for the room to settle before
    learning cooling, but don't learn an overrun from it."""
    room = Room(analytics)
    room.run(30, rate=0.6, heated=False)
    room.run(60, rate=-0.3, heated=False)
    room.run(1, rate=1.5)
    assert learned(analytics).overrun_stretches == 0
    assert learned(analytics).cooling_rate == pytest.approx(-0.3, abs=0.02)


def test_overrun_added_to_the_log(analytics):
    heat_then_overrun(Room(analytics))
    [entry] = analytics.get_log("z", "r")
    assert entry["overrun_rise"] == pytest.approx(0.4, abs=0.02)
    assert entry["overrun_minutes"] == pytest.approx(39, abs=2)


def test_overrun_survives_a_restart(analytics):
    heat_then_overrun(Room(analytics))
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_history(analytics.get_history_for_storage())
    restored.restore_log(analytics.get_log_for_storage())
    assert restored.rooms["z"]["r"].overrun_rise == learned(analytics).overrun_rise
    assert restored.get_log("z", "r")[0]["overrun_rise"] == analytics.get_log("z", "r")[0]["overrun_rise"]


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------

def test_still_warming_after_heating_has_no_cooling_estimate(analytics):
    room = Room(analytics)
    heat_then_overrun(room)                                  # learns the overrun
    room.run(40, rate=1.5)
    room.run(10, rate=0.6, heated=False)                     # 10 minutes into the next overrun
    data = room.result(target=room.true_temp - 0.5, needs_heating=False)
    assert data.eta_minutes is None
    assert data.eta_status == "still_warming"
    assert data.current_overrun is True
    assert data.rate_so_far == pytest.approx(0.6, abs=0.05)
    assert data.expected_peak == pytest.approx(room.analytics.rooms["z"]["r"].stretch.start_temperature + 0.4, abs=0.03)
    assert data.expected_peak_minutes == pytest.approx(29, abs=3)


def test_warming_from_something_else_has_no_cooling_estimate(analytics):
    """Cooking, sun: not heated and past the overrun, but the room is warming."""
    room = Room(analytics)
    settled_after_heat(room)
    room.run(30, rate=0.8, heated=False)
    data = room.result(target=room.true_temp - 0.5, needs_heating=False)
    assert data.eta_minutes is None
    assert data.eta_status == "not_cooling"


def test_cooling_estimate_once_cooling(analytics):
    room = Room(analytics)
    settled_after_heat(room)
    data = room.result(target=room.true_temp - 0.3, needs_heating=False)
    assert data.eta_status == "cooling"
    assert data.eta_minutes == pytest.approx(60, abs=5)       # 0.3 °C at 0.3 °C/h


def test_warming_estimate_status(analytics):
    room = Room(analytics)
    heat_then_overrun(room)
    data = room.result(target=room.true_temp + 1.0, needs_heating=True)
    assert data.eta_status == "warming" and data.eta_minutes is not None


def test_no_estimate_beyond_the_next_target_change(analytics):
    room = Room(analytics)
    settled_after_heat(room)
    target = room.true_temp - 0.3                             # about 60 minutes away
    args = ("z", "r", room.true_temp, target, False)
    soon = analytics.get_analytics(*args, now=room.now, target_changes_at=room.now + timedelta(minutes=30))
    assert soon.eta_minutes is None and soon.eta_timestamp is None
    assert soon.eta_status == "not_before_target_changes"
    later = analytics.get_analytics(*args, now=room.now, target_changes_at=room.now + timedelta(minutes=90))
    assert later.eta_status == "cooling" and later.eta_minutes is not None


def test_overrun_shown_once_learned_from_enough_stretches():
    analytics = HeatingAnalytics(min_stretches=2, learning_rate=0.3)
    room = Room(analytics)
    heat_then_overrun(room)
    assert room.result().overrun_rise is None
    heat_then_overrun(room)
    assert room.result().overrun_rise == pytest.approx(0.4, abs=0.03)
    assert room.result().overrun_stretches == 2


# ---------------------------------------------------------------------------
# When the target next changes
# ---------------------------------------------------------------------------

SCHEDULE = {"weekday": [{"start": "06:00", "temperature": 19.5}, {"start": "21:00", "temperature": 17.0}],
            "weekend": [{"start": "06:00", "temperature": 19.5}, {"start": "21:00", "temperature": 17.0}]}


@pytest.fixture
async def running(hass, add_trvs, make_coordinator, freezer):
    async def _build(hour):
        freezer.move_to(local_dt(2026, 1, 14, hour))          # a Wednesday
        await add_trvs(FakeTRV("room_trv", current_temperature=19.0))
        set_temp(hass, "sensor.room", 19.0)
        coordinator = make_coordinator(make_config({"zone_1": {
            "name": "Zone 1", "schedule": SCHEDULE,
            "rooms": {"room": make_room("Room", ["climate.room_trv"], ["sensor.room"])},
        }}))
        await coordinator.async_refresh()
        zone_data = coordinator.data["zone_1"]
        return coordinator, zone_data, zone_data["rooms"]["room"]

    return _build


async def test_target_changes_at_the_next_schedule_change(running):
    coordinator, zone_data, room_data = await running(12)
    when = coordinator._target_changes_at("zone_1", "room", room_data, local_dt(2026, 1, 14, 12))
    assert when == local_dt(2026, 1, 14, 21)


async def test_target_changes_tomorrow(running):
    coordinator, zone_data, room_data = await running(22)
    when = coordinator._target_changes_at("zone_1", "room", room_data, local_dt(2026, 1, 14, 22))
    assert when == local_dt(2026, 1, 15, 6)


async def test_target_changes_when_a_boost_ends(running):
    coordinator, zone_data, room_data = await running(12)
    end = local_dt(2026, 1, 14, 13)
    room_data = {**room_data, "boost": {"temperature": 22.0, "end_time": end}}
    assert coordinator._target_changes_at("zone_1", "room", room_data, local_dt(2026, 1, 14, 12)) == end


async def test_no_target_change_in_away_mode(running):
    coordinator, zone_data, room_data = await running(12)
    coordinator.away_mode = True
    assert coordinator._target_changes_at("zone_1", "room", room_data, local_dt(2026, 1, 14, 12)) is None


async def test_attributes(hass, running):
    from custom_components.heating_manager.climate import RoomClimate

    coordinator, zone_data, room_data = await running(12)
    entity = RoomClimate(coordinator, "zone_1", "room", coordinator.config["zones"]["zone_1"]["rooms"]["room"])
    entity.hass = hass
    attrs = entity.extra_state_attributes["heating_analytics"]
    assert set(attrs["overrun"]) == {"rise", "minutes", "stretches"}
    assert "overrun" in attrs["current"]
    assert {"status", "expected_peak", "expected_peak_minutes"} <= set(attrs["estimated_time_to_target"])
