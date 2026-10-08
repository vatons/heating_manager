"""Shared fixtures for Heating Manager tests.

Tests run against a real Home Assistant core (pytest-homeassistant-custom-component).
TRVs are simulated with a small in-test climate platform (``fake_trv``) so that
Home Assistant's own ``climate.set_temperature`` validation (min/max range,
hvac mode handling, etc.) is exercised exactly as it would be in production.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.heating_manager.coordinator import HeatingManagerCoordinator
from custom_components.heating_manager.entry_data import zones_to_subentries
from custom_components.heating_manager.const import (
    DEFAULT_ANALYTICS_HISTORY_SIZE,
    DEFAULT_ANALYTICS_MIN_SAMPLES,
    DEFAULT_BOOST_DURATION,
    DEFAULT_DERIVATIVE_SMOOTHING,
    DEFAULT_HEATING_DEADBAND,
    DEFAULT_TRV_COOLDOWN_OFFSET,
    DEFAULT_TRV_OFFSET_EMA_ALPHA,
    DEFAULT_TRV_OVERSHOOT_MAX,
    DEFAULT_TRV_OVERSHOOT_THRESHOLD,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/heating_manager in every test."""
    yield


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def local_dt(year=2026, month=1, day=14, hour=12, minute=0, second=0) -> datetime:
    """Return an aware datetime in Home Assistant's configured time zone.

    2026-01-14 is a Wednesday (weekday); 2026-01-17 is a Saturday.
    """
    return datetime(year, month, day, hour, minute, second, tzinfo=dt_util.DEFAULT_TIME_ZONE)


# ---------------------------------------------------------------------------
# Fake TRV climate platform
# ---------------------------------------------------------------------------

class FakeTRV(ClimateEntity):
    """A minimal TRV: reports an internal temperature and accepts setpoints."""

    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF, HVACMode.AUTO]

    def __init__(
        self,
        object_id: str,
        current_temperature: float | None = 18.0,
        target_temperature: float = 16.0,
        min_temp: float = 5.0,
        max_temp: float = 30.0,
        hvac_mode: HVACMode = HVACMode.HEAT,
        target_temperature_step: float | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self.entity_id = f"climate.{object_id}"
        self._attr_unique_id = f"fake_trv_{object_id}"
        self._attr_name = object_id
        self._attr_current_temperature = current_temperature
        self._attr_target_temperature = target_temperature
        self._attr_min_temp = min_temp
        self._attr_max_temp = max_temp
        self._attr_hvac_mode = hvac_mode
        self._attr_target_temperature_step = target_temperature_step
        self.fail_with = fail_with
        self.set_temperature_calls: list[float] = []
        self.hvac_mode_calls: list[HVACMode] = []

    @property
    def valve_open(self) -> bool:
        """Whether a real TRV would be calling for heat (opening its valve)."""
        return (
            self.hvac_mode == HVACMode.HEAT
            and self.current_temperature is not None
            and self.target_temperature is not None
            and self.target_temperature > self.current_temperature
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        temp = kwargs[ATTR_TEMPERATURE]
        self.set_temperature_calls.append(temp)
        # Like most TRVs, a setpoint sent while OFF is stored but not acted on.
        self._attr_target_temperature = temp
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self.hvac_mode_calls.append(hvac_mode)
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        """Simulate the TRV dropping off the network (e.g. flat battery) and returning."""
        self._attr_available = available
        self.async_write_ha_state()

    def set_internal_temperature(self, value: float | None) -> None:
        self._attr_current_temperature = value
        self.async_write_ha_state()


@pytest.fixture
def add_trvs(hass: HomeAssistant):
    """Return an async helper that registers FakeTRV entities in HA.

    Usage: ``trvs = await add_trvs(FakeTRV("kitchen_trv", ...), ...)``
    Returns a dict of entity_id -> FakeTRV.
    """
    registered: dict[str, FakeTRV] = {}

    async def _add(*entities: FakeTRV) -> dict[str, FakeTRV]:
        async def _setup_platform(hass, config, async_add_entities, discovery_info=None):
            async_add_entities(list(entities))

        mock_integration(hass, MockModule("fake_trv"))
        mock_platform(hass, "fake_trv.climate", MockPlatform(async_setup_platform=_setup_platform))
        assert await async_setup_component(
            hass, "climate", {"climate": [{"platform": "fake_trv"}]}
        )
        await hass.async_block_till_done()
        for ent in entities:
            registered[ent.entity_id] = ent
        return registered

    return _add


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------

def set_temp(hass: HomeAssistant, entity_id: str, value: float | str, **attrs) -> None:
    """Set a temperature sensor state (last_updated = now)."""
    hass.states.async_set(entity_id, str(value), {"unit_of_measurement": "°C", **attrs})


def set_last_seen(hass: HomeAssistant, entity_id: str, when: datetime) -> None:
    """Set a Zigbee2MQTT style last_seen timestamp sensor."""
    hass.states.async_set(entity_id, when.isoformat())


# ---------------------------------------------------------------------------
# Configuration builders
# ---------------------------------------------------------------------------

ALL_DAY_19 = {
    "weekday": [{"start": "00:00", "end": "23:59", "temperature": 19.0}],
    "weekend": [{"start": "00:00", "end": "23:59", "temperature": 19.0}],
}


def make_room(
    name: str,
    trvs: list[str] | None = None,
    sensors: list | None = None,
    **extra,
) -> dict:
    room = {"name": name}
    if trvs is not None:
        room["trvs"] = trvs
    if sensors is not None:
        room["sensors"] = sensors
    room.update(extra)
    return room


def make_config(zones: dict, **top_level) -> dict:
    return {"zones": zones, **top_level}


def single_room_config(
    trvs=("climate.room_trv",),
    sensors=("sensor.room_temperature",),
    schedule=None,
    **room_extra,
) -> dict:
    return make_config(
        {
            "zone_1": {
                "name": "Zone 1",
                "schedule": schedule if schedule is not None else ALL_DAY_19,
                "rooms": {
                    "room": make_room("Room", list(trvs), list(sensors), **room_extra),
                },
            }
        }
    )


@pytest.fixture
async def make_coordinator(hass: HomeAssistant):
    """Build a HeatingManagerCoordinator with sensible defaults.

    Coordinators are shut down at teardown so no refresh debouncer timers linger.
    """
    created: list[HeatingManagerCoordinator] = []

    def _make(config: dict, **overrides) -> HeatingManagerCoordinator:
        kwargs = dict(
            update_interval=60,
            minimum_temp=10.0,
            frost_protection_temp=5.0,
            fallback_mode="zone_average",
            boost_duration=DEFAULT_BOOST_DURATION,
            heating_deadband=DEFAULT_HEATING_DEADBAND,
            trv_overshoot_enabled=True,
            trv_overshoot_max=DEFAULT_TRV_OVERSHOOT_MAX,
            trv_overshoot_threshold=DEFAULT_TRV_OVERSHOOT_THRESHOLD,
            trv_cooldown_offset=DEFAULT_TRV_COOLDOWN_OFFSET,
            trv_offset_ema_alpha=DEFAULT_TRV_OFFSET_EMA_ALPHA,
            analytics_enabled=True,
            analytics_history_size=DEFAULT_ANALYTICS_HISTORY_SIZE,
            analytics_min_samples=DEFAULT_ANALYTICS_MIN_SAMPLES,
            derivative_smoothing=DEFAULT_DERIVATIVE_SMOOTHING,
        )
        kwargs.update(overrides)
        coordinator = HeatingManagerCoordinator(hass, config, **kwargs)
        created.append(coordinator)
        return coordinator

    yield _make
    for coordinator in created:
        await coordinator.async_shutdown()


def entry_from_options(options: dict, **kwargs) -> MockConfigEntry:
    """A current-version config entry from the 2.x options layout ({"settings", "zones"}).

    Zones and rooms become subentries, as they are after migration.
    """
    return MockConfigEntry(
        domain="heating_manager",
        title="Heating Manager",
        data={},
        version=2,
        options={"settings": dict(options.get("settings") or {})},
        subentries_data=zones_to_subentries(options.get("zones") or {}),
        **kwargs,
    )
