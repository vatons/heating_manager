"""Data coordinator for Heating Manager."""
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HEATING_DEMAND_MODE,
    CONF_ROOMS,
    CONF_SCHEDULE,
    CONF_TEMPERATURE_OFFSET,
    DEFAULT_BOOST_TEMP_INCREASE,
    DEFAULT_HEATING_DEMAND_MODE,
    DEFAULT_MAX_HEATING_DURATION,
    DOMAIN,
    MAX_BOOST_TEMP,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .boost_manager import BoostManager
from .heating_analytics import HeatingAnalytics
from .heating_logic import HeatingLogic
from .schedule_manager import ScheduleManager
from .temperature_manager import TemperatureManager
from .trv_controller import TRVController
from .trv_manager import TRVManager

_LOGGER = logging.getLogger(__name__)

STATE_SAVE_DELAY = 300  # seconds; learned state is written at most this often


class _HeatingManagerStore(Store):
    """Store subclass that provides v1->v2 migration without requiring the
    migration_func constructor parameter added in newer HA versions."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict,
    ) -> dict:
        if old_major_version == 1:
            _LOGGER.info("Migrating heating_manager storage from v1 to v2")
            # v1->v2: room_heating_state format changed from flat dict to nested.
            # HeatingLogic.restore_state handles the old format transparently.
            return old_data
        raise NotImplementedError(
            f"Cannot migrate heating_manager storage from version {old_major_version}"
        )


class HeatingManagerCoordinator(DataUpdateCoordinator):
    """Coordinator to manage heating logic and state."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict,
        update_interval: int,
        minimum_temp: float,
        frost_protection_temp: float,
        fallback_mode: str,
        boost_duration: int,
        heating_deadband: float,
        trv_overshoot_enabled: bool,
        trv_overshoot_max: float,
        trv_overshoot_threshold: float,
        trv_cooldown_offset: float,
        trv_offset_ema_alpha: float,
        analytics_enabled: bool,
        analytics_history_size: int,
        analytics_min_samples: int,
        derivative_smoothing: float,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )

        self.config = config
        self.minimum_temp = minimum_temp
        self.frost_protection_temp = frost_protection_temp
        self.fallback_mode = fallback_mode
        self.boost_duration = boost_duration
        self.heating_deadband = heating_deadband
        self.heating_demand_mode = config.get(CONF_HEATING_DEMAND_MODE, DEFAULT_HEATING_DEMAND_MODE)
        self._store = _HeatingManagerStore(hass, STORAGE_VERSION, STORAGE_KEY)

        # Runtime state
        self.away_mode = False
        self.manual_zone_temp: dict[str, dict] = {}  # zone_id -> {temperature, last_scheduled_temp}
        self.manual_room_temp: dict[str, dict[str, dict]] = {}  # zone_id -> room_id -> {temperature, last_scheduled_temp}
        self._loaded_state = False
        self._zone_heating_start: dict[str, Any] = {}  # zone_id -> datetime when demand started
        self.rooms_off: dict[str, list[str]] = {}  # zone_id -> room_ids switched off by the user

        # Initialize manager components
        self.temperature_manager = TemperatureManager(hass, fallback_mode)
        self.schedule_manager = ScheduleManager(minimum_temp)
        self.heating_logic = HeatingLogic(heating_deadband)
        self.boost_manager = BoostManager(hass, boost_duration)

        # Initialize TRV controller and manager
        self.trv_controller = TRVController(
            hass=hass,
            enabled=trv_overshoot_enabled,
            max_boost=trv_overshoot_max,
            overshoot_threshold=trv_overshoot_threshold,
            cooldown_offset=trv_cooldown_offset,
            ema_alpha=trv_offset_ema_alpha,
        )
        self.trv_manager = TRVManager(self.trv_controller)

        # Initialize heating analytics
        if analytics_enabled:
            self.heating_analytics = HeatingAnalytics(
                history_size=analytics_history_size,
                min_samples=analytics_min_samples,
                smoothing=derivative_smoothing,
            )
        else:
            self.heating_analytics = None

    async def _async_update_data(self) -> dict:
        """Fetch data from sensors and update heating logic."""
        try:
            # Load persistent state on first run; flag is set before the await
            # so a concurrent refresh cannot enter _load_state() a second time.
            if not self._loaded_state:
                self._loaded_state = True
                await self._load_state()

            zones = self.config.get("zones", {})
            current_time = dt_util.now()
            _state_changed = False  # Track whether any override was auto-expired
            # Collect expired overrides to delete after iterating — avoids mutating
            # manual_room_temp / manual_zone_temp while inside an async loop where
            # an await point between check and delete could cause a race.
            _expired_room_overrides: list[tuple[str, str]] = []  # (zone_id, room_id)
            _expired_zone_overrides: list[str] = []  # zone_id

            result = {}

            for zone_id, zone_config in zones.items():
                if not isinstance(zone_config, dict):
                    _LOGGER.error(
                        "Zone '%s' config must be a mapping, got %s — skipping",
                        zone_id, type(zone_config).__name__,
                    )
                    continue

                zone_data = {
                    "rooms": {},
                    "schedule": zone_config.get(CONF_SCHEDULE, {}),
                    "name": zone_config.get("name", zone_id),
                    "heating_demand": False,
                }

                rooms = zone_config.get(CONF_ROOMS, {})
                if not isinstance(rooms, dict):
                    _LOGGER.error(
                        "Zone '%s': 'rooms' must be a mapping, got %s — skipping zone",
                        zone_id, type(rooms).__name__,
                    )
                    result[zone_id] = zone_data
                    continue

                for room_id, room_config in rooms.items():
                    if not isinstance(room_config, dict):
                        _LOGGER.error(
                            "Zone '%s', room '%s' config must be a mapping, got %s — skipping",
                            zone_id, room_id, type(room_config).__name__,
                        )
                        continue

                    # Get room temperature from sensors with metadata
                    room_temp, temp_metadata = await self.temperature_manager.get_room_temperature(
                        zone_id, room_id, room_config, zones
                    )

                    # Check for boost
                    boost_info = self.boost_manager.get_boost_info(zone_id, room_id, current_time)

                    # Get target temperature - priority: away > boost > manual room > manual zone > schedule.
                    # The room's temperature_offset only adjusts targets the room inherits
                    # (schedule, zone override); away, boost and a room's own manual
                    # temperature are used exactly as set.
                    if self.away_mode:
                        target_temp = self.frost_protection_temp
                        _LOGGER.debug(
                            "Zone %s / Room %s: Using away mode temp: %.1f°C",
                            zone_id,
                            room_id,
                            target_temp,
                        )
                    elif boost_info:
                        target_temp = boost_info["temperature"]
                        _LOGGER.debug(
                            "Zone %s / Room %s: Using boost temp: %.1f°C",
                            zone_id,
                            room_id,
                            target_temp,
                        )
                    elif zone_id in self.manual_room_temp and room_id in self.manual_room_temp[zone_id]:
                        room_manual_info = self.manual_room_temp[zone_id][room_id]
                        scheduled_temp = self.schedule_manager.get_scheduled_temperature(
                            zone_config, current_time
                        )
                        if scheduled_temp != room_manual_info.get("last_scheduled_temp"):
                            _expired_room_overrides.append((zone_id, room_id))
                            target_temp = self._room_default_temperature(
                                zone_id, room_id, zone_config, room_config, current_time,
                                _expired_zone_overrides,
                            )
                            _state_changed = True
                            _LOGGER.debug(
                                "Zone %s / Room %s: Schedule changed, cleared room manual override, using %.1f°C",
                                zone_id,
                                room_id,
                                target_temp,
                            )
                        else:
                            target_temp = room_manual_info["temperature"]
                            _LOGGER.debug(
                                "Zone %s / Room %s: Using room manual temp: %.1f°C",
                                zone_id,
                                room_id,
                                target_temp,
                            )
                    else:
                        target_temp = self._room_default_temperature(
                            zone_id, room_id, zone_config, room_config, current_time,
                            _expired_zone_overrides,
                        )
                        if zone_id in _expired_zone_overrides:
                            _state_changed = True

                    # Safety check: ensure target_temp is never None
                    if target_temp is None:
                        _LOGGER.error(
                            "CRITICAL: target_temp is None for Zone %s / Room %s. "
                            "This should not happen. Using minimum_temp as fallback.",
                            zone_id,
                            room_id,
                        )
                        target_temp = self.minimum_temp

                    room_off = self.is_room_off(zone_id, room_id)

                    # Determine if room needs heating using smart deadband logic
                    needs_heating = self.heating_logic.calculate_heating_need(
                        zone_id, room_id, room_temp, target_temp
                    )

                    if room_off:
                        # Switched off by the user: hold TRVs at minimum and never demand heat
                        needs_heating = False
                        for trv_id in room_config.get("trvs", []):
                            await self.trv_controller.set_trv_setpoint(trv_id, self.minimum_temp)
                    else:
                        # Set TRV temperatures
                        await self.trv_manager.set_trv_temperatures(
                            zone_id, room_id, room_config, target_temp, room_temp, needs_heating,
                            # Only learn TRV offsets against the room's own sensors
                            learn_offset=temp_metadata["source"] == "local_sensors",
                        )

                    # Collect TRV offset information for display
                    trv_offset_info = await self.trv_manager.get_trv_offset_info(
                        self.hass, zone_id, room_id, room_config, room_temp
                    )

                    # Record temperature for analytics (if enabled and temp is valid)
                    if self.heating_analytics is not None and room_temp is not None:
                        self.heating_analytics.record_temperature(
                            zone_id, room_id, room_temp, needs_heating, current_time
                        )

                        # Get analytics data
                        analytics_data = self.heating_analytics.get_analytics(
                            zone_id, room_id, room_temp, target_temp, needs_heating
                        )
                        analytics_dict = analytics_data.to_dict()
                    else:
                        analytics_dict = None

                    # Extract sensor entity IDs for backwards compatibility in attributes
                    sensor_entity_ids = self.temperature_manager.get_sensor_entity_ids(room_config)

                    zone_data["rooms"][room_id] = {
                        "name": room_config.get("name", room_id),
                        "temperature": room_temp,
                        "target_temperature": target_temp,
                        "boost": boost_info,
                        "needs_heating": needs_heating,
                        "off": room_off,
                        "trvs": room_config.get("trvs", []),
                        "sensors": sensor_entity_ids,
                        "temperature_source": temp_metadata["source"],
                        "sensors_status": temp_metadata["sensors_status"],
                        "temperature_last_seen": temp_metadata["last_seen"],
                        "trv_offset_info": trv_offset_info,
                        "heating_analytics": analytics_dict,
                        "manual_room_override": {
                            "active": (
                                zone_id in self.manual_room_temp
                                and room_id in self.manual_room_temp.get(zone_id, {})
                            ),
                            "temperature": (
                                self.manual_room_temp.get(zone_id, {})
                                .get(room_id, {})
                                .get("temperature")
                            ),
                        },
                    }

                # Calculate zone heating demand based on configured mode
                zone_demand_mode = zone_config.get(
                    CONF_HEATING_DEMAND_MODE, self.heating_demand_mode
                )
                zone_data["heating_demand"] = self.heating_logic.calculate_zone_heating_demand(
                    zone_data["rooms"], zone_demand_mode, zone_id=zone_id
                )
                zone_data["heating_demand_mode"] = zone_demand_mode
                zone_data["manual_zone_override"] = {
                    "active": zone_id in self.manual_zone_temp,
                    "temperature": self.manual_zone_temp.get(zone_id, {}).get("temperature"),
                }

                # Pre-compute schedule period info once per update so
                # ZoneClimate.extra_state_attributes can read cached data
                # rather than recomputing on every HA state query.
                _current_period, _next_period = self.schedule_manager.get_period_info(
                    zone_config, current_time
                )
                zone_data["schedule_info"] = {
                    "current_temperature": self.schedule_manager.get_scheduled_temperature(
                        zone_config, current_time
                    ),
                    "current_period": _current_period,
                    "next_period": _next_period,
                }

                # Watchdog: track continuous heating demand duration
                if zone_data["heating_demand"]:
                    if zone_id not in self._zone_heating_start:
                        self._zone_heating_start[zone_id] = current_time
                    else:
                        duration_minutes = (
                            current_time - self._zone_heating_start[zone_id]
                        ).total_seconds() / 60
                        if duration_minutes > DEFAULT_MAX_HEATING_DURATION:
                            _LOGGER.critical(
                                "Zone %s has been demanding heat for %.0f minutes "
                                "(threshold: %d min). Possible sensor failure, stuck boost, "
                                "or unreachable target temperature. Clearing overrides and "
                                "setting all zone TRVs to minimum temperature.",
                                zone_id,
                                duration_minutes,
                                DEFAULT_MAX_HEATING_DURATION,
                            )
                            # Clear boost and manual overrides so the next update cycle
                            # does not immediately re-apply the high setpoints that
                            # caused or sustained the watchdog condition.
                            self.boost_manager.boost_state.pop(zone_id, None)
                            self.manual_room_temp.pop(zone_id, None)
                            self.manual_zone_temp.pop(zone_id, None)
                            # Reset timer first so the safeguard repeats only if the
                            # condition persists, even if a TRV command below fails
                            self._zone_heating_start[zone_id] = current_time
                            for room_id, room_data in zone_data["rooms"].items():
                                for trv_id in room_data.get("trvs", []):
                                    await self.trv_controller.set_trv_setpoint(
                                        trv_id, self.minimum_temp
                                    )
                            # Persist cleared state immediately
                            await self._save_state()
                else:
                    self._zone_heating_start.pop(zone_id, None)

                result[zone_id] = zone_data

            # Apply deferred override deletions collected during the room loop
            for zone_id, room_id in _expired_room_overrides:
                if zone_id in self.manual_room_temp:
                    self.manual_room_temp[zone_id].pop(room_id, None)
                    if not self.manual_room_temp[zone_id]:
                        del self.manual_room_temp[zone_id]
            for zone_id in _expired_zone_overrides:
                self.manual_zone_temp.pop(zone_id, None)

            if _state_changed:
                await self._save_state()
            else:
                # Learned TRV offsets, heating state and analytics change every
                # update; persist them periodically (and on shutdown) so they
                # survive restarts.
                self._store.async_delay_save(self._state_data, STATE_SAVE_DELAY)

            return result

        except Exception as err:
            _LOGGER.exception("Error updating heating manager data: %s", err)
            raise UpdateFailed(f"Error updating data: {err}")

    def _apply_room_offset(
        self, zone_id: str, room_id: str, room_config: dict, temperature: float
    ) -> float:
        """Apply a room's temperature_offset, clamped to safe bounds."""
        temperature_offset = room_config.get(CONF_TEMPERATURE_OFFSET, 0.0)
        if not temperature_offset:
            return temperature
        if abs(temperature_offset) > 5.0:
            _LOGGER.warning(
                "Zone %s / Room %s: temperature_offset %.1f°C is unusually large. "
                "Check configuration — the result will be clamped to safe bounds.",
                zone_id, room_id, temperature_offset,
            )
        adjusted = temperature + temperature_offset
        # Clamp: never below frost_protection_temp, never above MAX_BOOST_TEMP
        adjusted = max(self.frost_protection_temp, min(adjusted, MAX_BOOST_TEMP))
        _LOGGER.debug(
            "Zone %s / Room %s: Applied temperature offset %.1f°C (%.1f°C -> %.1f°C)",
            zone_id, room_id, temperature_offset, temperature, adjusted,
        )
        return adjusted

    def _room_default_temperature(
        self,
        zone_id: str,
        room_id: str,
        zone_config: dict,
        room_config: dict,
        current_time: datetime,
        expired_zone_overrides: list[str] | None = None,
    ) -> float:
        """Target a room inherits: the zone override (if still valid) or the schedule, plus offset.

        A zone override whose schedule period has ended is not used; its
        zone_id is appended to expired_zone_overrides when that list is given.
        """
        scheduled_temp = self.schedule_manager.get_scheduled_temperature(
            zone_config, current_time
        )
        base = scheduled_temp
        manual_info = self.manual_zone_temp.get(zone_id)
        if manual_info is not None:
            if scheduled_temp == manual_info.get("last_scheduled_temp"):
                base = manual_info["temperature"]
                _LOGGER.debug(
                    "Zone %s / Room %s: Using zone manual temp: %.1f°C", zone_id, room_id, base
                )
            elif expired_zone_overrides is not None and zone_id not in expired_zone_overrides:
                expired_zone_overrides.append(zone_id)
                _LOGGER.debug(
                    "Zone %s: Schedule changed, cleared zone manual override", zone_id
                )
        return self._apply_room_offset(zone_id, room_id, room_config, base)

    def get_room_default_temperature(
        self, zone_id: str, room_id: str, current_time: datetime | None = None
    ) -> float | None:
        """Public: the room's target with no room-level override (schedule or zone override + offset)."""
        zone_config = self.config.get("zones", {}).get(zone_id)
        if not isinstance(zone_config, dict):
            return None
        room_config = zone_config.get(CONF_ROOMS, {}).get(room_id)
        if not isinstance(room_config, dict):
            return None
        return self._room_default_temperature(
            zone_id, room_id, zone_config, room_config, current_time or dt_util.now()
        )

    async def _default_boost_temperature(self, zone_id: str, room_id: str) -> float | None:
        """Default boost: the room's current target, or its temperature if higher, + 2°C."""
        base = self.get_room_default_temperature(zone_id, room_id)
        if base is None:
            return None
        manual = self.manual_room_temp.get(zone_id, {}).get(room_id, {}).get("temperature")
        if manual is not None:
            base = max(base, manual)
        zones = self.config.get("zones", {})
        room_temp, _ = await self.temperature_manager.get_room_temperature(
            zone_id, room_id, zones[zone_id][CONF_ROOMS][room_id], zones
        )
        if room_temp is not None:
            base = max(base, room_temp)
        return base + DEFAULT_BOOST_TEMP_INCREASE

    def is_room_off(self, zone_id: str, room_id: str) -> bool:
        """Whether the user has switched this room off."""
        return room_id in self.rooms_off.get(zone_id, [])

    async def set_room_off(self, zone_id: str, room_id: str, off: bool) -> None:
        """Switch a room off (TRVs held at minimum, no heat demand) or back on."""
        rooms = self.rooms_off.setdefault(zone_id, [])
        if off and room_id not in rooms:
            rooms.append(room_id)
        elif not off and room_id in rooms:
            rooms.remove(room_id)
        if not rooms:
            self.rooms_off.pop(zone_id, None)
        _LOGGER.info("Room %s/%s switched %s", zone_id, room_id, "off" if off else "on")
        await self._save_state()
        await self.async_request_refresh()

    async def set_boost(
        self,
        zone_id: str,
        room_id: str,
        duration: int | None = None,
        temperature: float | None = None,
    ) -> None:
        """Set boost mode for a room."""
        if temperature is None:
            temperature = await self._default_boost_temperature(zone_id, room_id)
        success = await self.boost_manager.set_boost(
            zone_id,
            room_id,
            self.config,
            duration,
            temperature,
            get_room_temp_callback=self.temperature_manager.get_room_temperature,
        )
        if not success:
            raise HomeAssistantError(
                f"Failed to set boost for {zone_id}/{room_id}: "
                "room not found, has no sensors, or room temperature is unavailable"
            )
        # Boosting is an explicit request for heat, so it switches an off room back on
        if self.is_room_off(zone_id, room_id):
            self.rooms_off[zone_id].remove(room_id)
            if not self.rooms_off[zone_id]:
                del self.rooms_off[zone_id]
        # Clear any manual room override so it doesn't linger while boost is active
        if zone_id in self.manual_room_temp and room_id in self.manual_room_temp[zone_id]:
            del self.manual_room_temp[zone_id][room_id]
            if not self.manual_room_temp[zone_id]:
                del self.manual_room_temp[zone_id]
        await self._save_state()
        await self.async_request_refresh()

    async def update_boost_temperature(
        self, zone_id: str, room_id: str, temperature: float
    ) -> None:
        """Update the target temperature of an active boost without changing its end time."""
        if self.boost_manager.update_temperature(zone_id, room_id, temperature):
            await self._save_state()
            await self.async_request_refresh()

    async def clear_boost(self, zone_id: str, room_id: str) -> None:
        """Clear boost mode for a room."""
        if self.boost_manager.clear_boost(zone_id, room_id):
            await self._save_state()
            await self.async_request_refresh()

    async def set_away_mode(self, enabled: bool) -> None:
        """Set away mode."""
        self.away_mode = enabled
        _LOGGER.info("Away mode %s", "enabled" if enabled else "disabled")
        await self._save_state()
        await self.async_request_refresh()

    async def clear_manual_zone_temperature(self, zone_id: str) -> None:
        """Clear manual temperature override for a zone, reverting to schedule."""
        if zone_id in self.manual_zone_temp:
            del self.manual_zone_temp[zone_id]
            _LOGGER.info("Manual temperature cleared for zone %s", zone_id)
            await self._save_state()
            await self.async_request_refresh()

    async def set_manual_room_temperature(
        self, zone_id: str, room_id: str, temperature: float
    ) -> None:
        """Set manual temperature for a specific room until next schedule change."""
        zones = self.config.get("zones", {})
        if zone_id not in zones:
            _LOGGER.error("Zone %s not found", zone_id)
            return
        if room_id not in zones[zone_id].get(CONF_ROOMS, {}):
            _LOGGER.error("Room %s not found in zone %s", room_id, zone_id)
            return

        zone_config = zones[zone_id]
        current_time = dt_util.now()
        current_scheduled_temp = self.schedule_manager.get_scheduled_temperature(
            zone_config, current_time
        )

        if zone_id not in self.manual_room_temp:
            self.manual_room_temp[zone_id] = {}

        self.manual_room_temp[zone_id][room_id] = {
            "temperature": temperature,
            "last_scheduled_temp": current_scheduled_temp,
        }

        _LOGGER.info(
            "Manual room temperature set for %s/%s: %.1f°C (until schedule changes)",
            zone_id,
            room_id,
            temperature,
        )
        await self._save_state()
        await self.async_request_refresh()

    async def clear_manual_room_temperature(self, zone_id: str, room_id: str) -> None:
        """Clear manual room temperature override, reverting to zone override or schedule."""
        if zone_id in self.manual_room_temp and room_id in self.manual_room_temp[zone_id]:
            del self.manual_room_temp[zone_id][room_id]
            if not self.manual_room_temp[zone_id]:
                del self.manual_room_temp[zone_id]
            _LOGGER.info("Manual room temperature cleared for %s/%s", zone_id, room_id)
            await self._save_state()
            await self.async_request_refresh()

    async def set_manual_zone_temperature(
        self, zone_id: str, temperature: float
    ) -> None:
        """Set manual temperature for a zone until next schedule change."""
        zones = self.config.get("zones", {})
        if zone_id not in zones:
            _LOGGER.error("Zone %s not found", zone_id)
            return

        zone_config = zones[zone_id]
        current_time = dt_util.now()
        current_scheduled_temp = self.schedule_manager.get_scheduled_temperature(
            zone_config, current_time
        )

        self.manual_zone_temp[zone_id] = {
            "temperature": temperature,
            "last_scheduled_temp": current_scheduled_temp,
        }

        _LOGGER.info(
            "Manual temperature set for zone %s: %.1f°C (until schedule changes)",
            zone_id,
            temperature,
        )
        await self._save_state()
        await self.async_request_refresh()

    @staticmethod
    def _migrate_storage_data(data: dict) -> dict:
        """Migrate storage data from older versions to the current schema.

        v1 -> v2: room_heating_state was stored as a flat dict; v2 wraps it in
        {"room_heating_state": ..., "zone_avg_heating_active": ...}.
        HeatingLogic.restore_state handles this transparently, but we record the
        migration so that _save_state immediately writes the new schema.
        """
        stored_version = data.get("version", 1)
        if stored_version < 2:
            _LOGGER.info(
                "Migrating heating manager storage from v%d to v%d",
                stored_version,
                STORAGE_VERSION,
            )
            # No structural change needed here: HeatingLogic.restore_state already
            # handles the old flat format via its backward-compat branch.
            data["version"] = STORAGE_VERSION
        return data

    async def _load_state(self) -> None:
        """Load persistent state from storage."""
        data = await self._store.async_load()

        if data:
            data = self._migrate_storage_data(data)

            self.away_mode = data.get("away_mode", False)
            self.manual_zone_temp = data.get("manual_zone_temp", {})
            self.manual_room_temp = data.get("manual_room_temp", {})
            self.rooms_off = data.get("rooms_off", {})

            # Restore boost state (only if not expired)
            stored_boost = data.get("boost_state", {})
            self.boost_manager.restore_state(stored_boost)

            # Restore room heating state
            stored_heating_state = data.get("room_heating_state", {})
            self.heating_logic.restore_state(stored_heating_state)

            # Restore TRV offset history
            trv_offset_history = data.get("trv_offset_history", {})
            self.trv_controller.restore_offset_history(trv_offset_history)

            # Restore analytics history (if analytics enabled)
            if self.heating_analytics is not None:
                analytics_history = data.get("analytics_history", {})
                self.heating_analytics.restore_history(analytics_history)

    async def async_save_state(self) -> None:
        """Save persistent state to storage now."""
        await self._save_state()

    async def _save_state(self) -> None:
        """Save persistent state to storage."""
        await self._store.async_save(self._state_data())

    def _state_data(self) -> dict:
        """Build the persistent state."""
        data = {
            "version": STORAGE_VERSION,
            "away_mode": self.away_mode,
            "boost_state": self.boost_manager.get_state_for_storage(),
            "manual_zone_temp": self.manual_zone_temp,
            "manual_room_temp": self.manual_room_temp,
            "rooms_off": self.rooms_off,
            "room_heating_state": self.heating_logic.get_state_for_storage(),
            "trv_offset_history": self.trv_controller.get_offset_history_for_storage(),
        }

        # Add analytics history if enabled
        if self.heating_analytics is not None:
            data["analytics_history"] = self.heating_analytics.get_history_for_storage()

        return data
