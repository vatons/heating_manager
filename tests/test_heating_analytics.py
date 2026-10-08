"""Tests for HeatingAnalytics."""
from datetime import timedelta

import pytest

from custom_components.heating_manager.heating_analytics import HeatingAnalytics

from .conftest import local_dt

T0 = local_dt(hour=8)


@pytest.fixture
def ha_(freezer):
    freezer.move_to(T0)
    return HeatingAnalytics(history_size=30, min_samples=3, smoothing=0.3)


def feed(an, temps, needs=True, start=T0, step_min=1):
    for i, t in enumerate(temps):
        an.record_temperature("z", "r", t, needs, start + timedelta(minutes=i * step_min))


def test_insufficient_samples(ha_):
    feed(ha_, [18.0, 18.1])
    assert ha_.calculate_heating_rate("z", "r") is None
    assert ha_.get_analytics("z", "r", 18.1, 20.0, True).trend == "insufficient_data"


def test_unknown_room(ha_):
    assert ha_.calculate_heating_rate("z", "nope") is None


def test_heating_rate_per_hour(ha_):
    feed(ha_, [18.0, 18.05, 18.10, 18.15, 18.20])     # 0.05/min = 3°C/h
    assert ha_.calculate_heating_rate("z", "r") == pytest.approx(3.0)
    assert ha_.calculate_cooling_rate("z", "r") is None


def test_cooling_rate_uses_only_non_heating_samples(ha_):
    feed(ha_, [20.0, 19.99, 19.98, 19.97], needs=False)  # -0.6°C/h
    assert ha_.calculate_cooling_rate("z", "r") == pytest.approx(-0.6)


def test_identical_timestamps_ignored(ha_):
    for t in (18.0, 18.5, 19.0):
        ha_.record_temperature("z", "r", t, True, T0)
    assert ha_.calculate_heating_rate("z", "r") is None


def test_trend_and_eta(ha_):
    feed(ha_, [18.0, 18.05, 18.10, 18.15, 18.20])
    data = ha_.get_analytics("z", "r", 18.2, 20.0, True)
    assert data.trend == "heating_rapidly"
    assert data.eta_minutes == 36                       # 1.8°C at 3°C/h
    assert data.eta_timestamp == T0 + timedelta(minutes=36)
    assert 0 < data.confidence <= 1


def test_eta_none_when_moving_away(ha_):
    feed(ha_, [18.0, 17.95, 17.90, 17.85])
    data = ha_.get_analytics("z", "r", 17.85, 20.0, True)
    assert data.eta_minutes is None
    assert data.confidence == 0.0


@pytest.mark.parametrize(
    ("rate", "trend"),
    [(None, "insufficient_data"), (1.5, "heating_rapidly"), (0.5, "heating_slowly"),
     (0.0, "stable"), (-0.5, "cooling_slowly"), (-1.5, "cooling_rapidly")],
)
def test_trend_descriptions(ha_, rate, trend):
    assert ha_._get_trend_description(rate) == trend


def test_history_bounded(ha_):
    feed(ha_, [18.0 + i * 0.01 for i in range(50)])
    assert len(ha_.temp_history["z"]["r"]) == 30


def test_storage_round_trip(ha_):
    feed(ha_, [18.0 + i * 0.05 for i in range(15)])
    ha_.get_analytics("z", "r", 18.7, 20.0, True)
    stored = ha_.get_history_for_storage()
    assert len(stored["z"]["r"]["history"]) == 10
    restored = HeatingAnalytics(30, 3, 0.3)
    restored.restore_history(stored)
    assert len(restored.temp_history["z"]["r"]) == 10
    assert restored.calculate_heating_rate("z", "r") == pytest.approx(3.0)


def test_restore_skips_corrupt_entries(ha_):
    ha_.restore_history({"z": {"r": {"history": [
        {"timestamp": T0.isoformat(), "temperature": 18.0},
        {"timestamp": "garbage", "temperature": 18.0},
        {"temperature": 18.0},
    ], "smoothed_rates": {"heating_rate": None, "cooling_rate": None}}}})
    assert len(ha_.temp_history["z"]["r"]) == 1


@pytest.mark.xfail(
    raises=KeyError,
    reason="BUG (robustness): restored empty smoothed_rates raises KeyError, which "
    "would fail every coordinator update",
)
def test_restore_without_smoothed_rates_then_analytics(ha_):
    """Older storage may lack smoothed_rates; analytics must still work afterwards."""
    ha_.restore_history({"z": {"r": {"history": []}}})
    feed(ha_, [18.0, 18.05, 18.10, 18.15])
    ha_.get_analytics("z", "r", 18.15, 20.0, True)
