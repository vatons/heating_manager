"""Climate platform for Heating Manager."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback, async_get_current_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_BOOST_DURATION,
    ATTR_BOOST_TEMP,
    DOMAIN,
    MAX_BOOST_TEMP,
    SERVICE_CLEAR_BOOST,
    SERVICE_SET_BOOST,
)
from .coordinator import HeatingManagerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict,
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict | None = None,
) -> None:
    """Set up the climate platform."""
    if DOMAIN not in hass.data or "coordinator" not in hass.data[DOMAIN]:
        _LOGGER.error(
            "Heating manager coordinator not found in hass.data. "
            "Ensure the integration is configured in configuration.yaml before the climate platform loads."
        )
        return
    coordinator: HeatingManagerCoordinator = hass.data[DOMAIN]["coordinator"]

    entities = []

    # Create a climate entity for each room
    for zone_id, zone_data in coordinator.config.get("zones", {}).items():
        rooms = zone_data.get("rooms", {})
        for room_id, room_config in rooms.items():
            entities.append(RoomClimate(coordinator, zone_id, room_id, room_config))

        # Create a zone climate entity for heating demand monitoring
        entities.append(ZoneClimate(coordinator, zone_id, zone_data))

    # Create global climate entity for overall heating demand monitoring
    entities.append(GlobalClimate(coordinator))

    async_add_entities(entities)

    # Register entity services
    platform = async_get_current_platform()

    platform.async_register_entity_service(
        SERVICE_SET_BOOST,
        {
            vol.Optional(ATTR_BOOST_DURATION): cv.positive_int,
            vol.Optional(ATTR_BOOST_TEMP): vol.All(
                vol.Coerce(float), vol.Range(min=5.0, max=MAX_BOOST_TEMP)
            ),
        },
        "async_set_boost_service",
    )

    platform.async_register_entity_service(
        SERVICE_CLEAR_BOOST,
        {},
        "async_clear_boost_service",
    )


class RoomClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity representing a heating room."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = ["schedule", "away", "boost"]

    def __init__(
        self,
        coordinator: HeatingManagerCoordinator,
        zone_id: str,
        room_id: str,
        room_config: dict,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)

        self._zone_id = zone_id
        self._room_id = room_id
        self._room_config = room_config
        self._attr_name = f"{room_config.get('name', room_id)} (HM)"
        self._attr_unique_id = f"{DOMAIN}_{zone_id}_{room_id}"
        self._hvac_mode = HVACMode.HEAT

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        if not self.coordinator.data:
            return None

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})
        room_data = rooms.get(self._room_id, {})

        return room_data.get("temperature")

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        if not self.coordinator.data:
            return None

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})
        room_data = rooms.get(self._room_id, {})

        return room_data.get("target_temperature")

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        """Return the current HVAC action."""
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        if not self.coordinator.data:
            return HVACAction.IDLE

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})
        room_data = rooms.get(self._room_id, {})

        # Check if this room needs heating
        if room_data.get("needs_heating", False):
            return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode."""
        if self.coordinator.away_mode:
            return "away"

        # Check if this room has an active boost
        if not self.coordinator.data:
            return "schedule"

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})
        room_data = rooms.get(self._room_id, {})

        if room_data.get("boost"):
            return "boost"

        return "schedule"

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 30.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return {}

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})
        room_data = rooms.get(self._room_id, {})

        boost = room_data.get("boost")

        attrs = {
            # Identification
            "zone_id": self._zone_id,
            "zone_name": zone_data.get("name"),
            "room_id": self._room_id,

            # Heating status
            "needs_heating": room_data.get("needs_heating", False),
            "away_mode": self.coordinator.away_mode,

            # Boost information (grouped)
            "boost": {
                "temperature": boost.get("temperature") if boost else None,
                "end_time": boost.get("end_time").isoformat() if boost and boost.get("end_time") else None,
                "duration_minutes": boost.get("duration") if boost else None,
                "time_remaining_minutes": self._calculate_time_remaining(boost) if boost else None,
            },

            # Manual override information
            "manual_override": room_data.get("manual_room_override", {"active": False, "temperature": None}),

            # Sensor information
            "sensors": room_data.get("sensors_status", []),
        }

        # Add TRV offset information for monitoring intelligent control
        trv_offset_info = room_data.get("trv_offset_info", {})
        if trv_offset_info:
            trv_list = []
            for trv_id, data in trv_offset_info.items():
                trv_entry = {
                    "entity_id": trv_id,
                    "internal_temp": round(data["trv_internal_temp"], 1) if data["trv_internal_temp"] is not None else None,
                    "current_offset": round(data["current_offset"], 1) if data["current_offset"] is not None else None,
                    "learned_offset": round(data["average_offset"], 1) if data["average_offset"] is not None else None,
                    "setpoint": round(data["trv_setpoint"], 1) if data["trv_setpoint"] is not None else None,
                }
                trv_list.append(trv_entry)

            attrs["trv_control"] = {
                "enabled": self.coordinator.trv_controller.enabled,
                "trvs": trv_list,
            }

        # Add heating analytics if available
        analytics = room_data.get("heating_analytics")
        if analytics:
            attrs["heating_analytics"] = {
                "heating_rate_per_hour": analytics.get("heating_rate"),
                "cooling_rate_per_hour": analytics.get("cooling_rate"),
                "estimated_time_to_target": {
                    "minutes": analytics.get("eta_minutes"),
                    "timestamp": analytics.get("eta_timestamp"),
                    "confidence_percent": round(analytics.get("confidence", 0) * 100) if analytics.get("confidence") is not None else None,
                },
                "temperature_trend": analytics.get("trend"),
                "samples_count": analytics.get("samples_count"),
            }

        return attrs

    def _calculate_time_remaining(self, boost: dict) -> int | None:
        """Calculate minutes remaining in boost."""
        from homeassistant.util import dt as dt_util

        if not boost or "end_time" not in boost:
            return None

        end_time = boost["end_time"]
        current_time = dt_util.now()

        if end_time > current_time:
            delta = end_time - current_time
            return int(delta.total_seconds() / 60)

        return 0

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new HVAC mode."""
        self._hvac_mode = hvac_mode

        if hvac_mode == HVACMode.OFF and self.coordinator.data:
            # Set all TRVs in this room to minimum temperature
            zone_data = self.coordinator.data.get(self._zone_id, {})
            rooms = zone_data.get("rooms", {})
            room_data = rooms.get(self._room_id, {})

            for trv_id in room_data.get("trvs", []):
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {"entity_id": trv_id, "temperature": self.coordinator.minimum_temp},
                    blocking=True,
                )

        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature.

        Behaviour depends on whether boost is active:

        With boost active:
          - temp == schedule  → clear boost + manual override (revert to schedule)
          - temp < schedule   → clear boost, set manual override at lower temp
          - temp > schedule   → keep boost but update its target temperature

        Without boost:
          - temp == schedule  → clear manual override (revert to schedule)
          - any other temp    → set manual override
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        from homeassistant.util import dt as dt_util
        current_time = dt_util.now()
        zone_config = self.coordinator.config.get("zones", {}).get(self._zone_id, {})
        scheduled_temp = self.coordinator.schedule_manager.get_scheduled_temperature(zone_config, current_time)

        # Check boost_manager directly — it's updated synchronously before the
        # coordinator refresh, so coordinator.data may still reflect the old state.
        boost_info = self.coordinator.boost_manager.get_boost_info(
            self._zone_id, self._room_id, current_time
        )

        _TEMP_TOLERANCE = 0.05  # °C — treat as "equal to schedule" within this margin

        if boost_info:
            if abs(temperature - scheduled_temp) <= _TEMP_TOLERANCE:
                await self.coordinator.clear_boost(self._zone_id, self._room_id)
                await self.coordinator.clear_manual_room_temperature(self._zone_id, self._room_id)
            elif temperature < scheduled_temp:
                await self.coordinator.clear_boost(self._zone_id, self._room_id)
                await self.coordinator.set_manual_room_temperature(self._zone_id, self._room_id, temperature)
            else:
                # Above schedule: keep boost running, just update the temperature
                await self.coordinator.update_boost_temperature(self._zone_id, self._room_id, temperature)
            return

        if abs(temperature - scheduled_temp) <= _TEMP_TOLERANCE:
            await self.coordinator.clear_manual_room_temperature(self._zone_id, self._room_id)
        else:
            await self.coordinator.set_manual_room_temperature(self._zone_id, self._room_id, temperature)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode == "away":
            await self.coordinator.set_away_mode(True)
        elif preset_mode == "boost":
            await self.coordinator.set_boost(
                self._zone_id,
                self._room_id,
                temperature=None,  # Uses default: current room temp + boost increase
            )
        elif preset_mode == "schedule":
            if self.coordinator.away_mode:
                await self.coordinator.set_away_mode(False)
            await self.coordinator.clear_boost(self._zone_id, self._room_id)
            await self.coordinator.clear_manual_room_temperature(self._zone_id, self._room_id)
        # Coordinator refresh triggered by the above calls will update state

    async def async_set_boost_service(
        self, duration: int | None = None, temperature: float | None = None
    ) -> None:
        """Service call to set boost for this room."""
        await self.coordinator.set_boost(
            self._zone_id, self._room_id, duration, temperature
        )

    async def async_clear_boost_service(self) -> None:
        """Service call to clear boost for this room."""
        await self.coordinator.clear_boost(self._zone_id, self._room_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class ZoneClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity representing a zone's heating demand with temperature control."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = ["schedule", "away", "boost"]

    def __init__(
        self,
        coordinator: HeatingManagerCoordinator,
        zone_id: str,
        zone_config: dict,
    ) -> None:
        """Initialize the zone climate entity."""
        super().__init__(coordinator)

        self._zone_id = zone_id
        self._zone_config = zone_config
        self._attr_name = f"{zone_config.get('name', zone_id)} Zone (HM)"
        self._attr_unique_id = f"{DOMAIN}_{zone_id}_zone"

    @property
    def current_temperature(self) -> float | None:
        """Return the average temperature across all rooms in the zone."""
        if not self.coordinator.data:
            return None

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})

        temps = []
        for room_data in rooms.values():
            room_temp = room_data.get("temperature")
            if room_temp is not None:
                temps.append(room_temp)

        if temps:
            return sum(temps) / len(temps)

        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the average target temperature across all rooms in the zone."""
        if not self.coordinator.data:
            _LOGGER.debug(
                "Zone %s: No coordinator data available for target temperature",
                self._zone_id,
            )
            return self.coordinator.minimum_temp if self.coordinator else None

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})

        targets = []
        for room_data in rooms.values():
            target = room_data.get("target_temperature")
            if target is not None:
                targets.append(target)

        if targets:
            return sum(targets) / len(targets)

        # Fallback: if no targets available, return minimum temp
        _LOGGER.warning(
            "Zone %s: No target temperatures available for any room, using minimum_temp as fallback",
            self._zone_id,
        )
        return self.coordinator.minimum_temp

    @property
    def hvac_mode(self) -> HVACMode:
        """Return HEAT mode (always on for monitoring)."""
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        """Return HEATING if zone has heating demand, otherwise IDLE."""
        if not self.coordinator.data:
            return HVACAction.IDLE

        zone_data = self.coordinator.data.get(self._zone_id, {})
        if zone_data.get("heating_demand", False):
            return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 30.0

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode."""
        if self.coordinator.away_mode:
            return "away"

        if not self.coordinator.data:
            return "schedule"

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})

        for room_data in rooms.values():
            if room_data.get("boost"):
                return "boost"

        return "schedule"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return {}

        zone_data = self.coordinator.data.get(self._zone_id, {})
        rooms = zone_data.get("rooms", {})

        rooms_needing_heat = []
        boosted_rooms = []

        for room_id, room_data in rooms.items():
            if room_data.get("needs_heating"):
                rooms_needing_heat.append(room_id)
            if room_data.get("boost"):
                boosted_rooms.append(room_id)

        attrs = {
            # Identification
            "zone_id": self._zone_id,

            # Heating status
            "heating_demand": zone_data.get("heating_demand", False),
            "heating_demand_mode": zone_data.get("heating_demand_mode", "any_room"),
            "away_mode": self.coordinator.away_mode,
            "rooms_needing_heat": rooms_needing_heat,

            # Boost information (grouped)
            "boost": {
                "active": len(boosted_rooms) > 0,
                "room_ids": boosted_rooms,
            },

            # Zone-level manual override
            "manual_override": zone_data.get("manual_zone_override", {"active": False, "temperature": None}),
        }

        # Schedule info is pre-computed by the coordinator update cycle;
        # reading it here avoids recomputing on every HA state query.
        attrs["schedule"] = zone_data.get("schedule_info", {})

        # Add aggregated TRV offset summary for zone monitoring
        zone_trv_summary = []
        for room_id, room_data in rooms.items():
            trv_offset_info = room_data.get("trv_offset_info", {})
            for trv_id, data in trv_offset_info.items():
                zone_trv_summary.append({
                    "room_id": room_id,
                    "entity_id": trv_id,
                    "internal_temp": round(data["trv_internal_temp"], 1) if data["trv_internal_temp"] is not None else None,
                    "current_offset": round(data["current_offset"], 1) if data["current_offset"] is not None else None,
                    "learned_offset": round(data["average_offset"], 1) if data["average_offset"] is not None else None,
                    "setpoint": round(data["trv_setpoint"], 1) if data["trv_setpoint"] is not None else None,
                })

        if zone_trv_summary:
            attrs["trv_control"] = {
                "enabled": self.coordinator.trv_controller.enabled,
                "trvs": zone_trv_summary,
            }

        return attrs

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature as a manual zone override.

        Clears any per-room manual overrides in the zone (boosts are preserved).
        Equal to schedule → clear zone manual override, revert to schedule.
        Any other value → set manual zone override (persists until schedule changes).
        Boost is only triggered via the boost preset or set_boost service.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        from homeassistant.util import dt as dt_util
        current_time = dt_util.now()
        zone_config = self.coordinator.config.get("zones", {}).get(self._zone_id, {})
        scheduled_temp = self.coordinator.schedule_manager.get_scheduled_temperature(zone_config, current_time)
        rooms = zone_config.get("rooms", {})

        _TEMP_TOLERANCE = 0.05

        for room_id in rooms:
            await self.coordinator.clear_manual_room_temperature(self._zone_id, room_id)
        if abs(temperature - scheduled_temp) <= _TEMP_TOLERANCE:
            await self.coordinator.clear_manual_zone_temperature(self._zone_id)
        else:
            await self.coordinator.set_manual_zone_temperature(self._zone_id, temperature)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode for the zone."""
        if preset_mode == "away":
            await self.coordinator.set_away_mode(True)
        elif preset_mode == "boost":
            zone_config = self.coordinator.config.get("zones", {}).get(self._zone_id, {})
            rooms = zone_config.get("rooms", {})
            for room_id, room_config in rooms.items():
                if room_config.get("sensors"):
                    await self.coordinator.set_boost(self._zone_id, room_id, temperature=None)
        elif preset_mode == "schedule":
            if self.coordinator.away_mode:
                await self.coordinator.set_away_mode(False)
            zone_config = self.coordinator.config.get("zones", {}).get(self._zone_id, {})
            rooms = zone_config.get("rooms", {})
            for room_id in rooms:
                await self.coordinator.clear_boost(self._zone_id, room_id)
                await self.coordinator.clear_manual_room_temperature(self._zone_id, room_id)
            await self.coordinator.clear_manual_zone_temperature(self._zone_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class GlobalClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity representing global heating demand across all zones with temperature control."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = ["schedule", "away", "boost"]

    def __init__(
        self,
        coordinator: HeatingManagerCoordinator,
    ) -> None:
        """Initialize the global climate entity."""
        super().__init__(coordinator)

        self._attr_name = "Global (HM)"
        self._attr_unique_id = f"{DOMAIN}_global"

    @property
    def current_temperature(self) -> float | None:
        """Return the average temperature across all zones."""
        if not self.coordinator.data:
            return None

        all_temps = []
        for zone_data in self.coordinator.data.values():
            rooms = zone_data.get("rooms", {})
            for room_data in rooms.values():
                room_temp = room_data.get("temperature")
                if room_temp is not None:
                    all_temps.append(room_temp)

        if all_temps:
            return sum(all_temps) / len(all_temps)

        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the average target temperature across all zones."""
        if not self.coordinator.data:
            _LOGGER.debug("Global: No coordinator data available for target temperature")
            return self.coordinator.minimum_temp if self.coordinator else None

        zone_configs = self.coordinator.config.get("zones", {})
        all_targets = []
        for zone_id, zone_data in self.coordinator.data.items():
            # Skip zones without a schedule — their target is just minimum_temp
            zone_config = zone_configs.get(zone_id, {})
            if not zone_config.get("schedule"):
                continue
            rooms = zone_data.get("rooms", {})
            for room_data in rooms.values():
                target = room_data.get("target_temperature")
                if target is not None:
                    all_targets.append(target)

        if all_targets:
            return sum(all_targets) / len(all_targets)

        # Fallback: if no targets available, return minimum temp
        _LOGGER.warning(
            "Global: No target temperatures available for any scheduled zone, using minimum_temp as fallback"
        )
        return self.coordinator.minimum_temp

    @property
    def hvac_mode(self) -> HVACMode:
        """Return HEAT mode (always on for monitoring)."""
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        """Return HEATING if ANY zone has heating demand, otherwise IDLE."""
        if not self.coordinator.data:
            return HVACAction.IDLE

        # OR logic: if any zone has heating demand, return HEATING
        for zone_data in self.coordinator.data.values():
            if zone_data.get("heating_demand", False):
                return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return 30.0

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode."""
        if self.coordinator.away_mode:
            return "away"

        if not self.coordinator.data:
            return "schedule"

        # If any room in any zone has an active boost, return "boost"
        for zone_data in self.coordinator.data.values():
            rooms = zone_data.get("rooms", {})
            for room_data in rooms.values():
                if room_data.get("boost"):
                    return "boost"

        return "schedule"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data:
            return {}

        zones_needing_heat = []
        all_rooms_needing_heat = []
        all_boosted_rooms = []

        for zone_id, zone_data in self.coordinator.data.items():
            if zone_data.get("heating_demand", False):
                zones_needing_heat.append(zone_id)

            rooms = zone_data.get("rooms", {})
            for room_id, room_data in rooms.items():
                if room_data.get("needs_heating"):
                    all_rooms_needing_heat.append(f"{zone_id}/{room_id}")
                if room_data.get("boost"):
                    all_boosted_rooms.append(f"{zone_id}/{room_id}")

        attrs = {
            # System status
            "away_mode": self.coordinator.away_mode,
            "total_zones": len(self.coordinator.data),
            "zones_demanding_heat": len(zones_needing_heat),

            # Heating demand
            "heating": {
                "zones_needing_heat": zones_needing_heat,
                "rooms_needing_heat": all_rooms_needing_heat,
            },

            # Boost information (grouped)
            "boost": {
                "active": len(all_boosted_rooms) > 0,
                "room_ids": all_boosted_rooms,
            },

            # Manual override summary
            "manual_override": {
                "active": any(
                    zone_data.get("manual_zone_override", {}).get("active", False)
                    for zone_data in self.coordinator.data.values()
                ),
                "zones": [
                    zone_id
                    for zone_id, zone_data in self.coordinator.data.items()
                    if zone_data.get("manual_zone_override", {}).get("active", False)
                ],
            },
        }

        # Add system-wide TRV offset statistics
        all_trv_data = []
        total_trvs = 0
        current_offsets = []
        learned_offsets = []

        for zone_id, zone_data in self.coordinator.data.items():
            rooms = zone_data.get("rooms", {})
            for room_id, room_data in rooms.items():
                trv_offset_info = room_data.get("trv_offset_info", {})
                for trv_id, data in trv_offset_info.items():
                    total_trvs += 1
                    all_trv_data.append({
                        "zone_id": zone_id,
                        "room_id": room_id,
                        "entity_id": trv_id,
                        "internal_temp": round(data["trv_internal_temp"], 1) if data["trv_internal_temp"] is not None else None,
                        "current_offset": round(data["current_offset"], 1) if data["current_offset"] is not None else None,
                        "learned_offset": round(data["average_offset"], 1) if data["average_offset"] is not None else None,
                        "setpoint": round(data["trv_setpoint"], 1) if data["trv_setpoint"] is not None else None,
                    })

                    # Collect offsets for averaging
                    if data["current_offset"] is not None:
                        current_offsets.append(data["current_offset"])
                    if data["average_offset"] is not None:
                        learned_offsets.append(data["average_offset"])

        # Calculate average offsets
        if all_trv_data:
            attrs["trv_control"] = {
                "enabled": self.coordinator.trv_controller.enabled,
                "total_trvs": total_trvs,
                "avg_current_offset": round(sum(current_offsets) / len(current_offsets), 1) if current_offsets else None,
                "avg_learned_offset": round(sum(learned_offsets) / len(learned_offsets), 1) if learned_offsets else None,
                "trvs": all_trv_data,
            }

        return attrs

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature as a manual zone override across all zones.

        Clears any per-room manual overrides in all zones (boosts are preserved).
        Equal to a zone's scheduled temp → clear that zone's manual override.
        Any other value → set manual zone override (persists until schedule changes).
        Boost is only triggered via the boost preset or set_boost service.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        from homeassistant.util import dt as dt_util
        current_time = dt_util.now()
        zones = self.coordinator.config.get("zones", {})

        _TEMP_TOLERANCE = 0.05

        for zone_id, zone_config in zones.items():
            scheduled_temp = self.coordinator.schedule_manager.get_scheduled_temperature(zone_config, current_time)
            rooms = zone_config.get("rooms", {})
            for room_id in rooms:
                await self.coordinator.clear_manual_room_temperature(zone_id, room_id)
            if abs(temperature - scheduled_temp) <= _TEMP_TOLERANCE:
                await self.coordinator.clear_manual_zone_temperature(zone_id)
            else:
                await self.coordinator.set_manual_zone_temperature(zone_id, temperature)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode for all zones."""
        if preset_mode == "away":
            await self.coordinator.set_away_mode(True)
        elif preset_mode == "boost":
            zones = self.coordinator.config.get("zones", {})
            for zone_id, zone_config in zones.items():
                rooms = zone_config.get("rooms", {})
                for room_id, room_config in rooms.items():
                    if room_config.get("sensors"):
                        await self.coordinator.set_boost(zone_id, room_id, temperature=None)
        elif preset_mode == "schedule":
            if self.coordinator.away_mode:
                await self.coordinator.set_away_mode(False)
            zones = self.coordinator.config.get("zones", {})
            for zone_id, zone_config in zones.items():
                rooms = zone_config.get("rooms", {})
                for room_id in rooms:
                    await self.coordinator.clear_boost(zone_id, room_id)
                    await self.coordinator.clear_manual_room_temperature(zone_id, room_id)
                await self.coordinator.clear_manual_zone_temperature(zone_id)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
