"""Temperature management for Heating Manager."""
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ROOMS,
    CONF_SENSORS,
    SENSOR_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class TemperatureManager:
    """Manages temperature sensor reading and zone average calculations."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the temperature manager."""
        self.hass = hass
        self.last_sensor_values: dict[str, dict[str, Any]] = {}  # entity_id -> {value, timestamp}

    async def get_room_temperature(
        self, zone_id: str, room_id: str, room_config: dict, all_zones: dict
    ) -> tuple[float | None, dict]:
        """Get the current temperature for a room with metadata.

        Returns:
            tuple: (temperature, metadata_dict)
            metadata_dict contains:
                - source: str ("local_sensors", "zone_average", "unavailable")
                - sensors_status: list of sensor status dicts
                - last_seen: datetime of most recent sensor reading
        """
        sensors = room_config.get(CONF_SENSORS, [])
        current_time = dt_util.now()

        metadata = {
            "source": "unavailable",
            "sensors_status": [],
            "last_seen": None,
        }

        if not sensors:
            # No sensors, fall back to zone average
            zone_temp = await self.get_zone_average_temperature(zone_id, all_zones)
            metadata["source"] = "zone_average"
            return zone_temp, metadata

        valid_temps = []
        sensors_status = []
        most_recent_time = None

        for sensor_config in sensors:
            # Support both old format (string) and new format (dict with temperature and optional last_seen)
            if isinstance(sensor_config, str):
                # Old format: just the sensor entity ID
                temp_sensor_id = sensor_config
                last_seen_sensor_id = None
            elif isinstance(sensor_config, dict):
                # New format: dict with 'temperature' and optional 'last_seen'
                temp_sensor_id = sensor_config.get("temperature")
                last_seen_sensor_id = sensor_config.get("last_seen")
            else:
                _LOGGER.warning("Invalid sensor configuration format: %s", sensor_config)
                continue

            if not temp_sensor_id:
                _LOGGER.warning("Missing temperature sensor in configuration: %s", sensor_config)
                continue

            state = self.hass.states.get(temp_sensor_id)
            sensor_info = {
                "entity_id": temp_sensor_id,
                "value": None,
                "last_seen": None,
                "last_seen_source": None,
                "status": "unavailable",
            }

            if state and state.state not in ("unknown", "unavailable"):
                try:
                    temp = float(state.state)

                    # Determine last_seen timestamp
                    # Priority: 1) last_seen sensor entity, 2) state.last_updated
                    last_updated = None

                    if last_seen_sensor_id:
                        # Try to get last_seen from the dedicated sensor
                        last_seen_state = self.hass.states.get(last_seen_sensor_id)
                        if last_seen_state and last_seen_state.state not in ("unknown", "unavailable"):
                            try:
                                # Parse ISO format datetime: YYYY-MM-DDTHH:MM:SS+00:00
                                last_updated = dt_util.parse_datetime(last_seen_state.state)
                                sensor_info["last_seen_source"] = "dedicated_sensor"
                                _LOGGER.debug(
                                    "Using dedicated last_seen sensor %s for %s: %s",
                                    last_seen_sensor_id,
                                    temp_sensor_id,
                                    last_updated,
                                )
                            except (ValueError, TypeError) as err:
                                _LOGGER.warning(
                                    "Failed to parse last_seen from %s: %s",
                                    last_seen_sensor_id,
                                    err,
                                )

                    # Fallback to state.last_updated if no dedicated sensor or parsing failed
                    if last_updated is None:
                        last_updated = state.last_updated
                        sensor_info["last_seen_source"] = "state_last_updated"

                    sensor_info["value"] = temp
                    sensor_info["last_seen"] = last_updated.isoformat()

                    # Track most recent sensor reading
                    if most_recent_time is None or last_updated > most_recent_time:
                        most_recent_time = last_updated

                    # Check if sensor is recent enough
                    if current_time - last_updated < SENSOR_TIMEOUT:
                        valid_temps.append(temp)
                        sensor_info["status"] = "active"
                        # Update last known value
                        self.last_sensor_values[temp_sensor_id] = {
                            "value": temp,
                            "timestamp": last_updated,
                        }
                    else:
                        sensor_info["status"] = "timeout"
                except (ValueError, TypeError):
                    _LOGGER.warning("Invalid temperature from sensor %s", temp_sensor_id)
                    sensor_info["status"] = "invalid"

            sensors_status.append(sensor_info)

        metadata["sensors_status"] = sensors_status
        if most_recent_time:
            metadata["last_seen"] = most_recent_time.isoformat()

        # If we have valid temps, return average
        if valid_temps:
            metadata["source"] = "local_sensors"
            return sum(valid_temps) / len(valid_temps), metadata

        # Try to use last known values if within timeout
        for sensor_config in sensors:
            # Extract temperature sensor ID from config (support both formats)
            if isinstance(sensor_config, str):
                temp_sensor_id = sensor_config
            elif isinstance(sensor_config, dict):
                temp_sensor_id = sensor_config.get("temperature")
            else:
                continue

            if not temp_sensor_id:
                continue

            if temp_sensor_id in self.last_sensor_values:
                last_data = self.last_sensor_values[temp_sensor_id]
                if current_time - last_data["timestamp"] < SENSOR_TIMEOUT:
                    metadata["source"] = "local_sensors"
                    metadata["last_seen"] = last_data["timestamp"].isoformat()
                    return last_data["value"], metadata

        # Fall back to zone average
        zone_temp = await self.get_zone_average_temperature(zone_id, all_zones)
        metadata["source"] = "zone_average"
        return zone_temp, metadata

    async def get_zone_average_temperature(
        self, zone_id: str, all_zones: dict
    ) -> float | None:
        """Calculate the average temperature for all rooms in a zone."""
        zone_config = all_zones.get(zone_id, {})
        rooms = zone_config.get(CONF_ROOMS, {})

        temps = []
        for room_id, room_config in rooms.items():
            sensors = room_config.get(CONF_SENSORS, [])
            for sensor_config in sensors:
                # Support both old format (string) and new format (dict)
                if isinstance(sensor_config, str):
                    temp_sensor_id = sensor_config
                elif isinstance(sensor_config, dict):
                    temp_sensor_id = sensor_config.get("temperature")
                else:
                    continue

                if not temp_sensor_id:
                    continue

                state = self.hass.states.get(temp_sensor_id)
                if state and state.state not in ("unknown", "unavailable"):
                    try:
                        temps.append(float(state.state))
                    except (ValueError, TypeError):
                        pass

        if temps:
            return sum(temps) / len(temps)

        return None

    def get_sensor_entity_ids(self, room_config: dict) -> list[str]:
        """Extract sensor entity IDs from room config for backwards compatibility.

        Returns list of temperature sensor entity IDs.
        """
        sensors_config = room_config.get(CONF_SENSORS, [])
        sensor_entity_ids = []

        for sensor_config in sensors_config:
            if isinstance(sensor_config, str):
                sensor_entity_ids.append(sensor_config)
            elif isinstance(sensor_config, dict):
                temp_id = sensor_config.get("temperature")
                if temp_id:
                    sensor_entity_ids.append(temp_id)

        return sensor_entity_ids
