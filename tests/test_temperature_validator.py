"""Tests for TemperatureValidator."""
import pytest

from custom_components.heating_manager.temperature_validator import TemperatureValidator


@pytest.fixture
def v():
    return TemperatureValidator(max_change_per_min=0.5)


@pytest.mark.parametrize(("temp", "ok"), [(-20.0, True), (50.0, True), (-20.1, False), (50.1, False), (21.0, True)])
def test_range(v, temp, ok):
    assert v.validate(temp) is ok


def test_first_reading_is_plausible(v):
    assert v.validate(21.0, None, None) is True


def test_plausible_change(v):
    assert v.validate(21.0, 20.0, 120) is True      # 1.0 in 2 min
    assert v.validate(22.0, 20.0, 120) is False     # 2.0 in 2 min


def test_zero_time_delta_with_same_value_is_plausible(v):
    """Re-reading an unchanged sensor in the same instant must not be rejected."""
    assert v.validate(20.0, 20.0, 0) is True


def test_negative_time_delta_rejected(v):
    assert v.validate(20.0, 19.0, -10) is False


def test_zero_time_delta_with_changed_value_rejected(v):
    assert v.validate(21.0, 20.0, 0) is False
