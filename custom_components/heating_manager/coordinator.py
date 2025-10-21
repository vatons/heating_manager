"""Data coordinator for Heating Manager."""
from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_HEATING_DEMAND_MODE,
    CONF_ROOMS,
    CONF_SCHEDULE,
    DEFAULT_HEATING_DEMAND_MODE,
    DOMAIN,
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
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
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
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

        # Runtime state
        self.away_mode = False
        self.manual_zone_temp: dict[str, dict] = {}  # zone_id -> {temperature, until_next_schedule}

        # Initialize manager components
        self.temperature_manager = TemperatureManager(hass)
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
        self.analytics_enabled = analytics_enabled
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
            # Load persistent state on first run
            if not hasattr(self, "_loaded_state"):
                await self._load_state()
                self._loaded_state = True

            zones = self.config.get("zones", {})
            current_time = dt_util.now()

            result = {}

            for zone_id, zone_config in zones.items():
                zone_data = {
                    "rooms": {},
                    "schedule": zone_config.get(CONF_SCHEDULE, {}),
                    "name": zone_config.get("name", zone_id),
                    "heating_demand": False,
                }

                rooms = zone_config.get(CONF_ROOMS, {})

                for room_id, room_config in rooms.items():
                    # Get room temperature from sensors with metadata
                    room_temp, temp_metadata = await self.temperature_manager.get_room_temperature(
                        zone_id, room_id, room_config, zones
                    )

                    # Check for boost
                    boost_info = self.boost_manager.get_boost_info(zone_id, room_id, current_time)

                    # Get target temperature - priority: away > boost > manual zone > schedule
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
                    elif zone_id in self.manual_zone_temp:
                        # Check if manual temp should still be active
                        manual_info = self.manual_zone_temp[zone_id]
                        scheduled_temp = self.schedule_manager.get_scheduled_temperature(
                            zone_config, current_time
                        )

                        # If schedule changed, clear manual override
                        if scheduled_temp != manual_info.get("last_scheduled_temp"):
                            del self.manual_zone_temp[zone_id]
                            target_temp = scheduled_temp
                            _LOGGER.debug(
                                "Zone %s / Room %s: Schedule changed, cleared manual override, using scheduled temp: %.1f°C",
                                zone_id,
                                room_id,
                                target_temp,
                            )
                        else:
                            target_temp = manual_info["temperature"]
                            _LOGGER.debug(
                                "Zone %s / Room %s: Using manual temp: %.1f°C",
                                zone_id,
                                room_id,
                                target_temp,
                            )
                    else:
                        target_temp = self.schedule_manager.get_scheduled_temperature(
                            zone_config, current_time
                        )
                        _LOGGER.debug(
                            "Zone %s / Room %s: Using scheduled temp: %.1f°C",
                            zone_id,
                            room_id,
                            target_temp,
                        )

                    # Safety check: ensure target_temp is never None
                    if target_temp is None:
                        _LOGGER.error(
                            "CRITICAL: target_temp is None for Zone %s / Room %s. "
                            "This should not happen. Using minimum_temp as fallback.",
                            zone_id,
                            room_id,
                        )
                        target_temp = self.minimum_temp

                    # Determine if room needs heating using smart deadband logic
                    needs_heating = self.heating_logic.calculate_heating_need(
                        zone_id, room_id, room_temp, target_temp
                    )

                    # Set TRV temperatures
                    await self.trv_manager.set_trv_temperatures(
                        zone_id, room_id, room_config, target_temp, room_temp, needs_heating
                    )

                    # Collect TRV offset information for display
                    trv_offset_info = await self.trv_manager.get_trv_offset_info(
                        self.hass, zone_id, room_id, room_config, room_temp
                    )

                    # Record temperature for analytics (if enabled and temp is valid)
                    if self.analytics_enabled and room_temp is not None:
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
                        "trvs": room_config.get("trvs", []),
                        "sensors": sensor_entity_ids,
                        "temperature_source": temp_metadata["source"],
                        "sensors_status": temp_metadata["sensors_status"],
                        "temperature_last_seen": temp_metadata["last_seen"],
                        "trv_offset_info": trv_offset_info,
                        "heating_analytics": analytics_dict,
                    }

                # Calculate zone heating demand based on configured mode
                zone_demand_mode = zone_config.get(
                    CONF_HEATING_DEMAND_MODE, self.heating_demand_mode
                )
                zone_data["heating_demand"] = self.heating_logic.calculate_zone_heating_demand(
                    zone_data["rooms"], zone_demand_mode
                )
                zone_data["heating_demand_mode"] = zone_demand_mode

                result[zone_id] = zone_data

            # Save state periodically
            await self._save_state()

            return result

        except Exception as err:
            _LOGGER.error("Error updating heating manager data: %s", err)
            raise UpdateFailed(f"Error updating data: {err}")

    async def set_boost(
        self,
        zone_id: str,
        room_id: str,
        duration: int | None = None,
        temperature: float | None = None,
    ) -> None:
        """Set boost mode for a room."""
        await self.boost_manager.set_boost(
            zone_id,
            room_id,
            self.config,
            duration,
            temperature,
            get_room_temp_callback=self.temperature_manager.get_room_temperature,
        )
        await self.async_request_refresh()

    async def clear_boost(self, zone_id: str, room_id: str) -> None:
        """Clear boost mode for a room."""
        if self.boost_manager.clear_boost(zone_id, room_id):
            await self.async_request_refresh()

    async def set_away_mode(self, enabled: bool) -> None:
        """Set away mode."""
        self.away_mode = enabled
        _LOGGER.info("Away mode %s", "enabled" if enabled else "disabled")
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
        await self.async_request_refresh()

    async def _load_state(self) -> None:
        """Load persistent state from storage."""
        data = await self._store.async_load()

        if data:
            self.away_mode = data.get("away_mode", False)
            self.manual_zone_temp = data.get("manual_zone_temp", {})

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
            if self.analytics_enabled and self.heating_analytics:
                analytics_history = data.get("analytics_history", {})
                self.heating_analytics.restore_history(analytics_history)

    async def _save_state(self) -> None:
        """Save persistent state to storage."""
        data = {
            "away_mode": self.away_mode,
            "boost_state": self.boost_manager.get_state_for_storage(),
            "manual_zone_temp": self.manual_zone_temp,
            "room_heating_state": self.heating_logic.get_state_for_storage(),
            "trv_offset_history": self.trv_controller.get_offset_history_for_storage(),
        }

        # Add analytics history if enabled
        if self.analytics_enabled and self.heating_analytics:
            data["analytics_history"] = self.heating_analytics.get_history_for_storage()

        await self._store.async_save(data)
