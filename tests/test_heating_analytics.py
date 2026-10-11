"""Tests for HeatingAnalytics: learning from whole heated / not heated stretches."""
from __future__ import annotations

from datetime import timedelta
import math

import pytest

from custom_components.heating_manager.heating_analytics import STORAGE_FORMAT, HeatingAnalytics, _trend

from .conftest import local_dt

T0 = local_dt(hour=8)
MINUTE = timedelta(minutes=1)


class Room:
    """Feeds one room's readings minute by minute, as the coordinator does."""

    def __init__(self, analytics: HeatingAnalytics, resolution: float = 0.01, temp: float = 18.0):
        self.analytics = analytics
        self.resolution = resolution
        self.true_temp = temp
        self.now = T0

    def run(self, minutes: int, rate: float = 0.0, heated: bool = True, flowing: bool | None = None,
            outdoor: float | None = None, own_sensors: bool = True, loss: float | None = None):
        """rate in °C/h; or loss (per hour per °C above outdoor) for exponential cooling."""
        for _ in range(minutes):
            reported = round(self.true_temp / self.resolution) * self.resolution
            self.analytics.record_temperature(
                "z", "r", reported, heated, self.now, heat_flowing=flowing,
                own_sensors=own_sensors, outdoor_temp=outdoor,
            )
            if loss is not None:
                self.true_temp -= loss * (self.true_temp - outdoor) / 60
            else:
                self.true_temp += rate / 60
            self.now += MINUTE
        return self

    def result(self, target: float = 21.0, needs_heating: bool = True, outdoor: float | None = None):
        reported = round(self.true_temp / self.resolution) * self.resolution
        return self.analytics.get_analytics("z", "r", reported, target, needs_heating, outdoor, now=self.now)


@pytest.fixture
def analytics():
    return HeatingAnalytics(min_stretches=1, learning_rate=0.3)


# ---------------------------------------------------------------------------
# Heating rate and warm-up
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("rate", "resolution", "minutes"),
    [
        (1.0, 0.01, 60),
        (1.0, 0.1, 60),     # a typical sensor: the old method read this as 0 °C/h
        (0.5, 0.1, 90),
        (2.0, 0.1, 45),
        (1.0, 0.5, 180),    # very coarse: needs a long stretch
    ],
)
def test_heating_rate_with_coarse_sensors(analytics, rate, resolution, minutes):
    room = Room(analytics, resolution=resolution)
    room.run(minutes, rate=rate).run(1, heated=False)          # the stretch ends
    learned = room.result().heating_rate
    assert learned == pytest.approx(rate, rel=0.15)


def test_warm_up_then_heating_rate(analytics):
    room = Room(analytics, resolution=0.1)
    room.run(15, rate=0.0).run(45, rate=1.2).run(1, heated=False)
    data = room.result()
    assert 13 <= data.warm_up_minutes <= 17       # true 15, though the sensor shows the rise later
    assert data.heating_rate == pytest.approx(1.2, rel=0.15)
    assert data.heating_stretches == 1


def test_warm_up_and_rate_with_a_noisy_sensor(analytics):
    """A sensor that flickers by a step now and then: one early flicker doesn't count as warming."""
    import random

    rng = random.Random(7)
    start = T0
    for minute in range(75):
        true = 18.0 + max(0, minute - 20) * 1.0 / 60          # 20 min warm-up, then 1 °C/h
        reported = round((true + rng.choice([0, 0, 0, 0.1, -0.1])) / 0.1) * 0.1
        if minute == 3:
            reported = 18.1                                   # an early flicker
        analytics.record_temperature("z", "r", reported, True, start + minute * MINUTE)
    analytics.record_temperature("z", "r", 19.0, False, start + 75 * MINUTE)
    data = analytics.get_analytics("z", "r", 19.0, 21.0, True, now=start + 76 * MINUTE)
    assert 14 <= data.warm_up_minutes <= 26
    assert data.heating_rate == pytest.approx(1.0, rel=0.25)


def test_no_warm_up_or_rate_from_short_stretches(analytics):
    room = Room(analytics)
    room.run(8, rate=2.0).run(1, heated=False)
    data = room.result()
    assert (data.heating_rate, data.warm_up_minutes, data.heating_stretches) == (None, None, 0)


def test_room_that_never_warms_learns_nothing(analytics):
    room = Room(analytics, resolution=0.1)
    room.run(60, rate=0.0).run(1, heated=False)
    assert room.result().heating_stretches == 0


def test_rates_learned_over_stretches(analytics):
    room = Room(analytics)
    room.run(60, rate=1.0).run(1, heated=False)
    room.run(60, rate=2.0).run(1, heated=False)
    # Each stretch moves the learned rate by the learning rate (0.3)
    assert room.result().heating_rate == pytest.approx(1.0 + 0.3 * (2.0 - 1.0), rel=0.05)
    assert room.result().heating_stretches == 2


def test_rates_hidden_until_enough_stretches():
    analytics = HeatingAnalytics(min_stretches=2, learning_rate=0.3)
    room = Room(analytics)
    room.run(60, rate=1.0).run(1, heated=False)
    assert room.result().heating_rate is None
    assert room.result().heating_stretches == 1
    room.run(60, rate=1.0).run(1, heated=False)
    assert room.result().heating_rate == pytest.approx(1.0, rel=0.05)


# ---------------------------------------------------------------------------
# Cooling and heat loss
# ---------------------------------------------------------------------------

def test_cooling_rate_skips_radiator_heat_after_heating(analytics):
    room = Room(analytics)
    room.run(30, rate=1.0)                              # heating
    room.run(15, rate=0.4, heated=False)                # radiators still warm: still rising
    room.run(60, rate=-0.5, heated=False)
    room.run(1, rate=0.0)                               # heat again: the cooling stretch ends
    assert room.result().cooling_rate == pytest.approx(-0.5, rel=0.1)


def test_wanting_heat_with_the_boiler_off_is_cooling(analytics):
    room = Room(analytics)
    room.run(60, rate=-0.6, heated=True, flowing=False).run(1, rate=0.0, heated=True, flowing=True)
    data = room.result()
    assert data.cooling_rate == pytest.approx(-0.6, rel=0.1)
    assert data.heating_stretches == 0


def test_heat_loss_per_degree_with_outdoor_temperature(analytics):
    room = Room(analytics, temp=20.0)
    room.analytics.record_temperature("z", "r", 20.0, False, T0 - MINUTE)   # not straight after heating
    room.run(120, heated=False, outdoor=5.0, loss=0.04).run(1, rate=0.0)
    data = room.result(outdoor=5.0)
    assert data.heat_loss_per_degree == pytest.approx(0.04, rel=0.1)
    assert data.cooling_rate == pytest.approx(-0.04 * 14.4, rel=0.15)


def test_no_heat_loss_without_outdoor_temperature(analytics):
    room = Room(analytics, temp=20.0)
    room.run(120, rate=-0.5, heated=False).run(1, rate=0.0)
    data = room.result()
    assert data.cooling_rate == pytest.approx(-0.5, rel=0.1)
    assert data.heat_loss_per_degree is None


def test_no_heat_loss_when_barely_warmer_than_outside(analytics):
    room = Room(analytics, temp=12.0)
    room.run(120, heated=False, outdoor=10.0, loss=0.05).run(1, rate=0.0)
    assert room.result().heat_loss_per_degree is None


def test_long_stretch_learned_in_parts(analytics):
    """A room that's never heated (e.g. all summer) still learns its cooling rate."""
    room = Room(analytics)
    room.run(200, rate=-0.3, heated=False)                 # no change of state at all
    assert room.result().cooling_stretches == 1
    assert room.result().cooling_rate == pytest.approx(-0.3, rel=0.1)


def test_heated_room_that_never_warms_doesnt_grow_without_limit(analytics):
    room = Room(analytics)
    room.run(400, rate=0.0)                                # e.g. a stuck valve, for hours
    stretch = analytics.rooms["z"]["r"].stretch
    assert len(stretch.readings) <= 181
    assert room.result().heating_stretches == 0


# ---------------------------------------------------------------------------
# What isn't learned from
# ---------------------------------------------------------------------------

def test_fallback_temperatures_end_the_stretch_unlearned(analytics):
    room = Room(analytics)
    room.run(40, rate=1.0)
    room.run(1, rate=1.0, own_sensors=False)               # sensors offline: zone average used
    room.run(40, rate=1.0).run(1, heated=False)
    # Only the second stretch (40 min) counted, not one 80-minute stretch
    assert room.result().heating_stretches == 1


def test_gap_ends_the_stretch_unlearned(analytics):
    room = Room(analytics)
    room.run(40, rate=1.0)
    room.now += timedelta(hours=2)                         # e.g. Home Assistant was off
    room.run(5, rate=1.0).run(1, heated=False)
    assert room.result().heating_stretches == 0


def test_skip_reading(analytics):
    room = Room(analytics)
    room.run(40, rate=1.0)
    analytics.skip_reading("z", "r")
    room.run(1, heated=False)
    assert room.result().heating_stretches == 0


# ---------------------------------------------------------------------------
# The stretch in progress
# ---------------------------------------------------------------------------

def test_current_stretch(analytics):
    room = Room(analytics)
    room.run(1, rate=0.0, heated=False)
    assert room.result().current_state == "not_heated"
    room.run(20, rate=1.5)
    data = room.result()
    assert (data.current_state, data.current_minutes) == ("heated", 19)
    assert data.rate_so_far == pytest.approx(1.5, rel=0.1)
    assert data.trend == "heating_rapidly"


def test_trend_from_learned_rate_before_the_stretch_has_a_rate(analytics):
    room = Room(analytics)
    room.run(60, rate=0.5).run(1, heated=False)
    room.run(30, rate=0.0, heated=False)
    room.run(2, rate=0.5)                                  # just started heating again
    data = room.result()
    assert data.rate_so_far is None
    assert data.trend == "heating_slowly"                  # from the learned 0.5 °C/h


@pytest.mark.parametrize(
    ("rate", "trend"),
    [(None, "insufficient_data"), (1.5, "heating_rapidly"), (0.5, "heating_slowly"),
     (0.0, "stable"), (-0.5, "cooling_slowly"), (-1.5, "cooling_rapidly")],
)
def test_trend_descriptions(rate, trend):
    assert _trend(rate) == trend


# ---------------------------------------------------------------------------
# Time to target
# ---------------------------------------------------------------------------

def learned_heating(analytics):
    room = Room(analytics, resolution=0.01)
    room.run(15, rate=0.0).run(60, rate=1.2).run(1, heated=False)
    return room


def test_time_to_target_includes_warm_up(analytics):
    room = learned_heating(analytics)
    room.true_temp = 18.0
    room.run(30, rate=0.0, heated=False)
    room.run(1, rate=0.0)                                  # heat has just started
    data = room.result(target=19.2)
    # About 15 min warm-up (minus the minute gone) + 1.2 °C at 1.2 °C/h
    assert 70 <= data.eta_minutes <= 78
    assert data.eta_timestamp is not None
    assert data.confidence > 0

    room.run(20, rate=1.2)                                 # warmed up and rising
    data = room.result(target=19.2)
    assert 30 <= data.eta_minutes <= 50                    # no warm-up left


def test_time_to_target_while_the_boiler_is_off(analytics):
    room = learned_heating(analytics)
    room.true_temp = 18.0
    room.run(5, rate=0.0, heated=True, flowing=False)
    data = room.result(target=19.2)
    # Assumes heat starts now: the full warm-up, then the climb
    assert 70 <= data.eta_minutes <= 80


def test_time_to_cool_with_heat_loss(analytics):
    room = Room(analytics, temp=20.0)
    analytics.record_temperature("z", "r", 20.0, False, T0 - MINUTE)
    room.run(120, heated=False, outdoor=5.0, loss=0.04).run(1, rate=0.0)
    loss = room.result(outdoor=5.0).heat_loss_per_degree
    data = analytics.get_analytics("z", "r", 19.0, 17.0, False, 5.0, now=room.now)
    expected = math.log((19.0 - 5.0) / (17.0 - 5.0)) / loss * 60
    assert data.eta_minutes == round(expected)


def test_time_to_cool_without_outdoor_temperature(analytics):
    room = Room(analytics)
    room.run(120, rate=-0.5, heated=False).run(1, rate=0.0)
    data = analytics.get_analytics("z", "r", 19.0, 18.0, False, None, now=room.now)
    assert data.eta_minutes == pytest.approx(120, abs=15)


def test_no_estimate_without_learned_rates(analytics):
    data = analytics.get_analytics("z", "r", 18.0, 20.0, True)
    assert (data.eta_minutes, data.eta_timestamp, data.confidence) == (None, None, 0.0)
    assert data.trend == "insufficient_data"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_storage_round_trip(analytics):
    room = learned_heating(analytics)
    room.run(60, rate=-0.4, heated=False).run(1, rate=0.0)
    stored = analytics.get_history_for_storage()
    assert stored["format"] == STORAGE_FORMAT
    restored = HeatingAnalytics(min_stretches=1, learning_rate=0.3)
    restored.restore_history(stored)
    before, after = room.result(), restored.get_analytics("z", "r", 18.0, 21.0, True, now=room.now)
    for field in ("heating_rate", "warm_up_minutes", "heating_stretches", "cooling_rate", "cooling_stretches"):
        assert getattr(after, field) == getattr(before, field), field
    assert after.current_state is None                     # the stretch in progress isn't saved


def test_older_storage_is_dropped(analytics):
    analytics.restore_history({"z": {"r": {"history": [
        {"timestamp": T0.isoformat(), "temperature": 18.0, "needs_heating": True},
    ], "smoothed_rates": {"heating_rate": 0.0, "cooling_rate": None}}}})
    assert analytics.rooms == {}


def test_restore_skips_corrupt_rooms(analytics):
    analytics.restore_history({"format": STORAGE_FORMAT, "rooms": {"z": {
        "good": {"heating_rate": 1.0, "heating_stretches": 3},
        "bad": {"heating_rate": "fast"},
    }}})
    assert analytics.rooms["z"]["good"].heating_rate == 1.0
    assert "bad" not in analytics.rooms["z"]


def test_after_a_restart_cooling_waits_for_radiators(analytics):
    """The state before a restart isn't known, so radiators may still be warm."""
    room = Room(analytics)
    room.run(10, rate=0.5, heated=False)                   # first readings: settling, not used
    room.run(30, rate=-0.5, heated=False).run(1, rate=0.0)
    assert room.result().cooling_stretches == 0            # only 25 min usable: too short
