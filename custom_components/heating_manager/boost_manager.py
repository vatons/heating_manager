"""Boost mode management for Heating Manager."""
from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ROOMS,
    CONF_SENSORS,
    CONF_ZONES,
    DEFAULT_BOOST_TEMP_INCREASE,
)

_LOGGER = logging.getLogger(__name__)


class BoostManager:
    """Manages boost mode state and operations."""

    def __init__(self, hass: HomeAssistant, boost_duration: int) -> None:
        """Initialize the boost manager."""
        self.hass = hass
        self.boost_duration = boost_duration
        self.boost_state: dict[str, dict[str, dict]] = {}  # zone_id -> room_id -> boost_info

    def get_boost_info(
        self, zone_id: str, room_id: str, current_time: datetime
    ) -> dict | None:
        """Get boost information if active."""
        if zone_id not in self.boost_state:
            return None

        if room_id not in self.boost_state[zone_id]:
            return None

        boost = self.boost_state[zone_id][room_id]

        # Check if boost has expired
        if current_time > boost["end_time"]:
            # Remove expired boost
            del self.boost_state[zone_id][room_id]
            if not self.boost_state[zone_id]:
                del self.boost_state[zone_id]
            return None

        return boost

    async def set_boost(
        self,
        zone_id: str,
        room_id: str,
        config: dict,
        duration: int | None = None,
        temperature: float | None = None,
        get_room_temp_callback=None,
    ) -> None:
        """Set boost mode for a room.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            config: Full integration config
            duration: Boost duration in minutes (uses default if None)
            temperature: Target boost temperature (calculated from current temp if None)
            get_room_temp_callback: Async callback to get room temperature (required if temperature is None)
        """
        # Verify room exists and has sensors
        zones = config.get(CONF_ZONES, {})
        if zone_id not in zones:
            _LOGGER.error("Zone %s not found", zone_id)
            return

        rooms = zones[zone_id].get(CONF_ROOMS, {})
        if room_id not in rooms:
            _LOGGER.error("Room %s not found in zone %s", room_id, zone_id)
            return

        # Check if room has sensors
        sensors = rooms[room_id].get(CONF_SENSORS, [])
        if not sensors:
            _LOGGER.error(
                "Cannot boost room %s in zone %s: no temperature sensors",
                room_id,
                zone_id,
            )
            return

        # Calculate boost parameters
        if duration is None:
            duration = self.boost_duration

        current_time = dt_util.now()
        end_time = current_time + timedelta(minutes=duration)

        if temperature is None:
            # Get current room temperature and add boost
            if get_room_temp_callback is None:
                _LOGGER.error(
                    "Cannot calculate boost temperature: no callback provided"
                )
                return

            room_temp, _ = await get_room_temp_callback(
                zone_id, room_id, rooms[room_id], zones
            )
            temperature = room_temp + DEFAULT_BOOST_TEMP_INCREASE
            _LOGGER.debug(
                "Boost temperature calculated from current room temp: %.1f°C + %.1f°C = %.1f°C",
                room_temp, DEFAULT_BOOST_TEMP_INCREASE, temperature
            )

        # Store boost state
        if zone_id not in self.boost_state:
            self.boost_state[zone_id] = {}

        self.boost_state[zone_id][room_id] = {
            "temperature": temperature,
            "end_time": end_time,
            "duration": duration,
        }

        _LOGGER.info(
            "Boost set for %s/%s: %.1f°C for %d minutes",
            zone_id,
            room_id,
            temperature,
            duration,
        )

    def clear_boost(self, zone_id: str, room_id: str) -> bool:
        """Clear boost mode for a room.

        Returns:
            True if boost was cleared, False if no boost was active
        """
        if zone_id in self.boost_state and room_id in self.boost_state[zone_id]:
            del self.boost_state[zone_id][room_id]
            if not self.boost_state[zone_id]:
                del self.boost_state[zone_id]

            _LOGGER.info("Boost cleared for %s/%s", zone_id, room_id)
            return True

        return False

    def get_state_for_storage(self) -> dict:
        """Serialize boost state for storage.

        Returns:
            Dictionary with boost state in serializable format
        """
        boost_serialized = {}
        for zone_id, rooms in self.boost_state.items():
            boost_serialized[zone_id] = {}
            for room_id, boost_info in rooms.items():
                boost_serialized[zone_id][room_id] = {
                    "temperature": boost_info["temperature"],
                    "end_time": boost_info["end_time"].isoformat(),
                    "duration": boost_info["duration"],
                }
        return boost_serialized

    def restore_state(self, stored_boost: dict) -> None:
        """Restore boost state from storage.

        Only restores boost entries that haven't expired.
        """
        current_time = dt_util.now()

        for zone_id, rooms in stored_boost.items():
            for room_id, boost_info in rooms.items():
                end_time = dt_util.parse_datetime(boost_info["end_time"])
                if end_time and end_time > current_time:
                    if zone_id not in self.boost_state:
                        self.boost_state[zone_id] = {}
                    self.boost_state[zone_id][room_id] = {
                        "temperature": boost_info["temperature"],
                        "end_time": end_time,
                        "duration": boost_info["duration"],
                    }
