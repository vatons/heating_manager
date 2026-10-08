"""Temperature management for Heating Manager."""
from datetime import datetime, timedelta
from statistics import median
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ROOMS,
    CONF_SENSORS,
    CONF_TRVS,
    FALLBACK_MODE_LAST_KNOWN,
    FALLBACK_MODE_TRV,
    FALLBACK_MODE_ZONE_AVERAGE,
    DEFAULT_MAX_TEMP_CHANGE_PER_MIN,
    SENSOR_TIMEOUT,
)
from .temperature_validator import TemperatureValidator
from .units import climate_attr_celsius, sensor_celsius

_LOGGER = logging.getLogger(__name__)


def state_last_reported(state) -> datetime:
    """Return when an entity last reported, even if its value did not change.

    state.last_updated only moves when the value or attributes change, so a
    sensor sitting at a steady temperature would look dead. last_reported
    (Home Assistant 2024.3+) moves on every report.
    """
    return getattr(state, "last_reported", None) or state.last_updated


class TemperatureManager:
    """Manages temperature sensor reading and zone average calculations."""

    def __init__(
        self, hass: HomeAssistant, fallback_mode: str = FALLBACK_MODE_ZONE_AVERAGE
    ) -> None:
        """Initialize the temperature manager.

        fallback_mode decides where a room's temperature comes from when it has
        no fresh sensor reading: "zone_average", "trv" or "last_known".
        """
        self.hass = hass
        if fallback_mode not in (
            FALLBACK_MODE_ZONE_AVERAGE, FALLBACK_MODE_TRV, FALLBACK_MODE_LAST_KNOWN
        ):
            _LOGGER.warning(
                "Unknown fallback_mode %r, using %r", fallback_mode, FALLBACK_MODE_ZONE_AVERAGE
            )
            fallback_mode = FALLBACK_MODE_ZONE_AVERAGE
        self.fallback_mode = fallback_mode
        self.last_sensor_values: dict[str, dict[str, Any]] = {}  # entity_id -> {value, timestamp}
        self._validator = TemperatureValidator(max_change_per_min=DEFAULT_MAX_TEMP_CHANGE_PER_MIN)

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
            # No sensors configured: the room's own TRVs are the best guess, then the zone
            return await self._fallback_temperature(
                zone_id, room_config, all_zones, metadata, prefer_trv=True
            )

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
                    temp = sensor_celsius(state)

                    # Validate reading is physically plausible
                    previous_data = self.last_sensor_values.get(temp_sensor_id)
                    previous_value = previous_data["value"] if previous_data else None
                    previous_time = previous_data["timestamp"] if previous_data else None
                    time_delta_seconds = (
                        (current_time - previous_time).total_seconds()
                        if previous_time is not None
                        else None
                    )
                    if not self._validator.validate(temp, previous_value, time_delta_seconds):
                        _LOGGER.warning(
                            "Sensor %s reading %.1f°C failed validation, skipping",
                            temp_sensor_id, temp,
                        )
                        sensor_info["status"] = "invalid"
                        sensors_status.append(sensor_info)
                        continue

                    # Determine last_seen timestamp
                    # Priority: 1) last_seen sensor entity, 2) state.last_reported
                    last_updated = None

                    if last_seen_sensor_id:
                        # Try to get last_seen from the dedicated sensor
                        last_seen_state = self.hass.states.get(last_seen_sensor_id)
                        if last_seen_state and last_seen_state.state not in ("unknown", "unavailable"):
                            try:
                                # Parse ISO format datetime: YYYY-MM-DDTHH:MM:SS+00:00
                                last_updated = dt_util.parse_datetime(last_seen_state.state)
                                if last_updated is not None and last_updated.tzinfo is None:
                                    # No UTC offset given: assume HA's local time zone
                                    last_updated = last_updated.replace(
                                        tzinfo=dt_util.DEFAULT_TIME_ZONE
                                    )
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

                    # Fallback to the state's own report time if no dedicated sensor or parsing failed
                    if last_updated is None:
                        last_updated = state_last_reported(state)
                        sensor_info["last_seen_source"] = "state_last_reported"

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

        # Combine fresh readings: the median with three or more sensors, so one
        # outlier (a sensor by a fridge, oven or window) can't skew the room; the
        # mean otherwise
        if valid_temps:
            metadata["source"] = "local_sensors"
            if len(valid_temps) >= 3:
                return median(valid_temps), metadata
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

        return await self._fallback_temperature(zone_id, room_config, all_zones, metadata)

    def _trv_temperature(self, room_config: dict) -> float | None:
        """Average internal temperature (°C) of the room's available TRVs."""
        trv_temps = []
        for trv_id in room_config.get(CONF_TRVS, []):
            state = self.hass.states.get(trv_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            value = climate_attr_celsius(self.hass, state, "current_temperature")
            if value is not None and self._validator.is_in_valid_range(value):
                trv_temps.append(value)
        return sum(trv_temps) / len(trv_temps) if trv_temps else None

    async def _fallback_temperature(
        self,
        zone_id: str,
        room_config: dict,
        all_zones: dict,
        metadata: dict,
        prefer_trv: bool = False,
    ) -> tuple[float | None, dict]:
        """Room temperature when no fresh sensor reading exists, per fallback_mode.

        prefer_trv (rooms with no sensors configured) uses the TRVs regardless of
        fallback_mode. "trv" and "last_known" fall back to the zone average if
        they have no data.
        """
        if prefer_trv or self.fallback_mode == FALLBACK_MODE_TRV:
            trv_temp = self._trv_temperature(room_config)
            if trv_temp is not None:
                metadata["source"] = "trv"
                return trv_temp, metadata

        if self.fallback_mode == FALLBACK_MODE_LAST_KNOWN:
            latest: tuple[datetime, float] | None = None
            for temp_sensor_id in self.get_sensor_entity_ids(room_config):
                candidates = []
                if temp_sensor_id in self.last_sensor_values:
                    known = self.last_sensor_values[temp_sensor_id]
                    candidates.append((known["timestamp"], known["value"]))
                state = self.hass.states.get(temp_sensor_id)
                if state is not None and state.state not in ("unknown", "unavailable"):
                    try:
                        value = sensor_celsius(state)
                    except (TypeError, ValueError):
                        value = None
                    if value is not None and self._validator.is_in_valid_range(value):
                        candidates.append((state_last_reported(state), value))
                for candidate in candidates:
                    if latest is None or candidate[0] > latest[0]:
                        latest = candidate
            if latest is not None:
                metadata["source"] = "last_known"
                metadata["last_seen"] = latest[0].isoformat()
                return latest[1], metadata

        zone_temp = await self.get_zone_average_temperature(zone_id, all_zones)
        metadata["source"] = "zone_average"
        return zone_temp, metadata

    async def get_zone_average_temperature(
        self, zone_id: str, all_zones: dict
    ) -> float | None:
        """Calculate the average temperature for all rooms in a zone.

        Only includes sensor readings that are within SENSOR_TIMEOUT so that
        stale cached state values do not corrupt the zone average used as a
        fallback for rooms without sensors.
        """
        zone_config = all_zones.get(zone_id, {})
        rooms = zone_config.get(CONF_ROOMS, {})
        current_time = dt_util.now()

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
                        temp = sensor_celsius(state)
                        if current_time - state_last_reported(state) < SENSOR_TIMEOUT:
                            temps.append(temp)
                    except (ValueError, TypeError):
                        pass

        if temps:
            return sum(temps) / len(temps)

        _LOGGER.warning(
            "Zone %s: no sensors with fresh readings available for zone average fallback",
            zone_id,
        )
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
