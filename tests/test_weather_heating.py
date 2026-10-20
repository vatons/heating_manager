"""Heating power: the radiators' own rate before the room's losses to outside. Unlike the
heating rate it doesn't change with the weather, so warming estimates use it, less the
losses at today's outdoor temperature."""
from __future__ import annotations

import pytest

from custom_components.heating_manager.heating_analytics import HeatingAnalytics

from .test_heating_analytics import MINUTE, T0

POWER = 2.5      # °C/h the radiators add
LOSS = 0.03      # °C/h lost per °C warmer than outside


class House:
    """One room, warming and cooling as physics says: rate = power - loss x (room - outside)."""

    def __init__(self, analytics: HeatingAnalytics, temp: float = 18.0):
        self.analytics = analytics
        self.temp = temp
        self.now = T0

    def run(self, minutes: int, heated: bool, outdoor: float | None, record_outdoor: bool = True):
        for _ in range(minutes):
            self.analytics.record_temperature(
                "z", "r", round(self.temp, 2), heated, self.now,
                outdoor_temp=outdoor if record_outdoor else None,
            )
            gain = POWER if heated else 0.0
            self.temp += (gain - LOSS * (self.temp - outdoor)) / 60
            self.now += MINUTE
        return self

    def cycles(self, count: int, outdoor: float, record_outdoor: bool = True):
        for _ in range(count):
            self.run(60, True, outdoor, record_outdoor)
            self.run(150, False, outdoor, record_outdoor)
        return self


@pytest.fixture
def analytics():
    return HeatingAnalytics(min_stretches=2, learning_rate=0.3)


def room(analytics):
    return analytics.rooms["z"]["r"]


def test_heating_power_learned_once_heat_loss_is_known(analytics):
    house = House(analytics)
    house.cycles(1, outdoor=12.0).run(1, True, 12.0)       # heat again: the cooling is learned
    assert room(analytics).heat_loss_stretches == 1
    assert room(analytics).heating_power is None            # heat loss not trusted yet
    house.cycles(4, outdoor=12.0)
    assert room(analytics).heat_loss == pytest.approx(LOSS, rel=0.15)
    assert room(analytics).heating_power == pytest.approx(POWER, rel=0.08)


def test_same_power_whatever_the_weather():
    """The heating rate learned in mild weather is too fast for a cold day; the power isn't."""
    mild, cold = (HeatingAnalytics(min_stretches=2, learning_rate=0.3) for _ in range(2))
    House(mild).cycles(6, outdoor=12.0)
    House(cold).cycles(6, outdoor=0.0)
    assert room(mild).heating_rate > room(cold).heating_rate + 0.2
    assert room(mild).heating_power == pytest.approx(room(cold).heating_power, rel=0.08)


def test_warming_estimate_uses_todays_outdoor_temperature(analytics):
    House(analytics).cycles(6, outdoor=12.0)                # learned in mild weather
    mild = analytics.get_analytics("z", "r", 18.0, 20.0, True, outdoor_temp=12.0)
    cold = analytics.get_analytics("z", "r", 18.0, 20.0, True, outdoor_temp=0.0)
    assert mild.expected_heating_rate == pytest.approx(POWER - LOSS * (19.0 - 12.0), rel=0.1)
    assert cold.expected_heating_rate == pytest.approx(POWER - LOSS * 19.0, rel=0.1)
    assert cold.eta_minutes > mild.eta_minutes * 1.15        # 2 °C at about 1.9 vs 2.3 °C/h
    assert cold.heating_power == pytest.approx(POWER, rel=0.08)


def test_without_outdoor_temperature_the_learned_rate_is_used(analytics):
    House(analytics).cycles(6, outdoor=12.0, record_outdoor=False)
    assert room(analytics).heating_power is None
    data = analytics.get_analytics("z", "r", 18.0, 20.0, True)
    assert data.expected_heating_rate is None
    assert data.eta_status == "warming" and data.eta_minutes is not None


def test_outdoor_known_now_but_no_power_yet_uses_the_learned_rate(analytics):
    House(analytics).cycles(6, outdoor=12.0, record_outdoor=False)
    data = analytics.get_analytics("z", "r", 18.0, 20.0, True, outdoor_temp=0.0)
    assert data.expected_heating_rate is None
    assert data.eta_minutes is not None


def test_too_cold_to_reach_target(analytics):
    House(analytics).cycles(6, outdoor=12.0)
    # 2.5 °C/h can't make up 0.03 x 100 °C of losses
    data = analytics.get_analytics("z", "r", 18.0, 20.0, True, outdoor_temp=-80.0)
    assert data.eta_minutes is None
    assert data.eta_status == "not_reaching"


def test_heating_power_survives_a_restart(analytics):
    House(analytics).cycles(6, outdoor=12.0)
    restored = HeatingAnalytics(min_stretches=2, learning_rate=0.3)
    restored.restore_history(analytics.get_history_for_storage())
    assert restored.rooms["z"]["r"].heating_power == room(analytics).heating_power
    assert restored.rooms["z"]["r"].heating_power_stretches == room(analytics).heating_power_stretches
