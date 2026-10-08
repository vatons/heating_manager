"""Temperature unit handling.

Heating Manager works in °C internally (schedules, settings, offsets). Values
are converted at the boundaries:
- temperature sensors report in their own unit_of_measurement;
- climate entities (TRVs) expose temperatures, min/max and step in Home
  Assistant's configured unit system, and set_temperature expects that unit.
"""
from __future__ import annotations

from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.util.unit_conversion import TemperatureConverter

_UNITS = {UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN}


def to_celsius(value: float, unit: str | None) -> float:
    """Convert a temperature in unit to °C (unknown or missing unit: assume °C)."""
    if unit in _UNITS and unit != UnitOfTemperature.CELSIUS:
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
    return value


def from_celsius(value: float, unit: str | None) -> float:
    """Convert a temperature in °C to unit."""
    if unit in _UNITS and unit != UnitOfTemperature.CELSIUS:
        return TemperatureConverter.convert(value, UnitOfTemperature.CELSIUS, unit)
    return value


def interval_to_celsius(value: float, unit: str | None) -> float:
    """Convert a temperature difference (e.g. a TRV's step size) to °C."""
    if unit in _UNITS and unit != UnitOfTemperature.CELSIUS:
        return TemperatureConverter.convert_interval(value, unit, UnitOfTemperature.CELSIUS)
    return value


def system_unit(hass: HomeAssistant) -> str:
    """The unit climate entities use for their attributes and services."""
    return hass.config.units.temperature_unit


def sensor_celsius(state: State) -> float:
    """A temperature sensor's state in °C. Raises ValueError/TypeError if not numeric."""
    return to_celsius(float(state.state), state.attributes.get("unit_of_measurement"))


def climate_attr_celsius(hass: HomeAssistant, state: State | None, attribute: str) -> float | None:
    """A climate entity temperature attribute (current_temperature, min_temp, ...) in °C."""
    if state is None:
        return None
    value: Any = state.attributes.get(attribute)
    if value is None:
        return None
    try:
        return to_celsius(float(value), system_unit(hass))
    except (TypeError, ValueError):
        return None
