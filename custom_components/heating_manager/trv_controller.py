"""TRV controller with intelligent setpoint calculation using sensor offset."""
from datetime import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class TRVController:
    """Manages TRV setpoint calculation using dynamic sensor offset tracking.

    This controller solves the problem of TRVs with internal temperature sensors
    that read warmer than the actual room temperature (due to proximity to radiator).

    By tracking the offset between the TRV's internal sensor and external room
    sensors, it calculates optimal TRV setpoints that compensate for this bias.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        enabled: bool = True,
        max_boost: float = 5.0,
        max_absolute_setpoint: float = 30.0,
        overshoot_threshold: float = 0.3,
        cooldown_offset: float = 1.0,
        ema_alpha: float = 0.15,
    ) -> None:
        """Initialize the TRV controller.

        Args:
            hass: Home Assistant instance
            enabled: Whether intelligent TRV control is enabled
            max_boost: Maximum boost to add above learned offset (°C)
            max_absolute_setpoint: Absolute maximum TRV setpoint (°C)
            overshoot_threshold: Temperature above target to trigger cooling (°C)
            cooldown_offset: Temperature below target to set for faster cooling (°C)
            ema_alpha: Exponential moving average smoothing factor (0.1=stable, 0.2=responsive)
        """
        self.hass = hass
        self.enabled = enabled
        self.max_boost = max_boost
        self.max_absolute_setpoint = max_absolute_setpoint
        self.overshoot_threshold = overshoot_threshold
        self.cooldown_offset = cooldown_offset
        self.ema_alpha = ema_alpha

        # Offset EMA storage: zone_id -> room_id -> trv_id -> float (EMA value)
        # Store per-TRV since different TRVs in same room may have different offsets
        self.offset_ema: dict[str, dict[str, dict[str, float]]] = {}

        # Default offset to use when no history available
        self.default_offset = 2.0

    def calculate_trv_setpoint(
        self,
        zone_id: str,
        room_id: str,
        trv_id: str,
        room_temp: float | None,
        target_temp: float,
        trv_internal_temp: float | None,
        needs_heating: bool,
    ) -> float:
        """Calculate optimal TRV setpoint using dynamic sensor offset.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            trv_id: TRV entity ID
            room_temp: External room sensor temperature (°C)
            target_temp: Desired room temperature (°C)
            trv_internal_temp: TRV's internal sensor temperature (°C)
            needs_heating: Whether room currently needs heating

        Returns:
            Optimal TRV setpoint temperature (°C)
        """
        # Disabled or missing data - use exact target
        if not self.enabled or room_temp is None or trv_internal_temp is None:
            return target_temp

        # Calculate current sensor offset
        current_offset = trv_internal_temp - room_temp

        # Update offset EMA for learning
        self._update_offset_ema(zone_id, room_id, trv_id, current_offset)

        # Get learned EMA offset for this TRV
        ema_offset = self._get_ema_offset(zone_id, room_id, trv_id)

        _LOGGER.debug(
            "TRV offset tracking - Zone: %s, Room: %s, TRV: %s | "
            "Room: %.1f°C, TRV Internal: %.1f°C, Current offset: %.1f°C, EMA offset: %.1f°C",
            zone_id, room_id, trv_id, room_temp, trv_internal_temp,
            current_offset, ema_offset
        )

        # Room is overshooting - cool down
        if room_temp > target_temp + self.overshoot_threshold:
            setpoint = target_temp - self.cooldown_offset  # Set low to cool faster
            _LOGGER.debug(
                "Room overshooting target, setting TRV low to cool: %.1f°C", setpoint
            )
            return max(setpoint, 5.0)  # Never go below 5°C for safety

        # Room at target - maintain with small boost
        if not needs_heating:
            maintain_boost = 0.5
            setpoint = target_temp + ema_offset + maintain_boost
            _LOGGER.debug(
                "Room at target, maintaining: target=%.1f°C + offset=%.1f°C + boost=%.1f°C = %.1f°C",
                target_temp, ema_offset, maintain_boost, setpoint
            )
            return min(setpoint, self.max_absolute_setpoint)

        # Room needs heating - calculate adaptive boost
        deficit = target_temp - room_temp

        if deficit > 3.0:
            # Large deficit - maximum boost for rapid heating
            boost = self.max_boost
            reason = "large deficit"
        elif deficit > 1.5:
            # Medium deficit - proportional boost
            boost = min(deficit * 1.5, self.max_boost)
            reason = "medium deficit (proportional)"
        elif deficit > 0.5:
            # Small deficit - moderate boost
            boost = 1.5
            reason = "small deficit"
        else:
            # Tiny deficit - minimal boost
            boost = 0.5
            reason = "tiny deficit"

        # Calculate final setpoint using EMA offset (already smoothed)
        setpoint = target_temp + ema_offset + boost

        # Apply safety cap
        setpoint = min(setpoint, self.max_absolute_setpoint)

        _LOGGER.debug(
            "Calculating TRV setpoint (%s): "
            "target=%.1f°C + ema_offset=%.1f°C + boost=%.1f°C = %.1f°C "
            "(deficit=%.1f°C, effective_overshoot=%.1f°C)",
            reason, target_temp, ema_offset, boost, setpoint,
            deficit, setpoint - target_temp
        )

        return setpoint

    def _update_offset_ema(
        self, zone_id: str, room_id: str, trv_id: str, offset: float
    ) -> None:
        """Update offset EMA for a specific TRV.

        Uses exponential moving average: EMA_new = α × current + (1 - α) × EMA_old
        """
        # Initialize nested dictionaries if needed
        if zone_id not in self.offset_ema:
            self.offset_ema[zone_id] = {}
        if room_id not in self.offset_ema[zone_id]:
            self.offset_ema[zone_id][room_id] = {}

        # If first reading, initialize with current offset
        if trv_id not in self.offset_ema[zone_id][room_id]:
            self.offset_ema[zone_id][room_id][trv_id] = offset
            _LOGGER.debug(
                "Initialized EMA for %s/%s/%s: %.1f°C (first reading)",
                zone_id, room_id, trv_id, offset
            )
        else:
            # Calculate EMA: new = α × current + (1 - α) × old
            old_ema = self.offset_ema[zone_id][room_id][trv_id]
            new_ema = (self.ema_alpha * offset) + ((1 - self.ema_alpha) * old_ema)
            self.offset_ema[zone_id][room_id][trv_id] = new_ema

            _LOGGER.debug(
                "Updated EMA for %s/%s/%s: %.1f°C -> %.1f°C (current: %.1f°C, α=%.2f)",
                zone_id, room_id, trv_id, old_ema, new_ema, offset, self.ema_alpha
            )

    def _get_ema_offset(
        self, zone_id: str, room_id: str, trv_id: str
    ) -> float:
        """Get EMA learned offset for a specific TRV."""
        ema = (
            self.offset_ema
            .get(zone_id, {})
            .get(room_id, {})
            .get(trv_id)
        )

        if ema is not None:
            _LOGGER.debug(
                "EMA offset for %s/%s/%s: %.1f°C",
                zone_id, room_id, trv_id, ema
            )
            return ema

        _LOGGER.debug(
            "No EMA history for %s/%s/%s, using default: %.1f°C",
            zone_id, room_id, trv_id, self.default_offset
        )
        return self.default_offset

    def get_offset_history_for_storage(self) -> dict[str, Any]:
        """Serialize offset EMA for persistent storage.

        Returns:
            Dictionary suitable for JSON serialization
        """
        return self.offset_ema

    def restore_offset_history(self, stored_history: dict[str, Any]) -> None:
        """Restore offset EMA from persistent storage.

        Args:
            stored_history: Previously serialized offset EMA data
        """
        if stored_history:
            # Check if this is old list-based history and convert to EMA
            if self._is_old_list_format(stored_history):
                _LOGGER.info("Converting old list-based offset history to EMA format")
                self.offset_ema = self._convert_old_history_to_ema(stored_history)
            else:
                # Already in EMA format
                self.offset_ema = stored_history

            _LOGGER.info(
                "Restored TRV offset EMA for %d zones",
                len(self.offset_ema)
            )

    def _is_old_list_format(self, data: dict[str, Any]) -> bool:
        """Check if stored data is in old list format."""
        for zone_data in data.values():
            if isinstance(zone_data, dict):
                for room_data in zone_data.values():
                    if isinstance(room_data, dict):
                        for trv_data in room_data.values():
                            # Old format: list of floats
                            # New format: single float
                            return isinstance(trv_data, list)
        return False

    def _convert_old_history_to_ema(self, old_history: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
        """Convert old list-based history to EMA format by averaging."""
        ema_data: dict[str, dict[str, dict[str, float]]] = {}

        for zone_id, zone_data in old_history.items():
            ema_data[zone_id] = {}
            for room_id, room_data in zone_data.items():
                ema_data[zone_id][room_id] = {}
                for trv_id, history_list in room_data.items():
                    if isinstance(history_list, list) and history_list:
                        # Convert list to average
                        avg = sum(history_list) / len(history_list)
                        ema_data[zone_id][room_id][trv_id] = avg
                        _LOGGER.debug(
                            "Converted %s/%s/%s: %d readings -> EMA %.1f°C",
                            zone_id, room_id, trv_id, len(history_list), avg
                        )

        return ema_data

    async def set_trv_temperature(
        self,
        zone_id: str,
        room_id: str,
        trv_id: str,
        target_temp: float,
        room_temp: float | None,
        needs_heating: bool,
    ) -> None:
        """Set TRV temperature with intelligent setpoint calculation.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            trv_id: TRV entity ID
            target_temp: Desired room temperature (°C)
            room_temp: External room sensor temperature (°C)
            needs_heating: Whether room currently needs heating
        """
        # Read TRV's internal temperature sensor
        trv_state = self.hass.states.get(trv_id)
        trv_internal_temp = None

        if trv_state and trv_state.attributes.get("current_temperature"):
            try:
                trv_internal_temp = float(
                    trv_state.attributes["current_temperature"]
                )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Invalid current_temperature from TRV %s", trv_id
                )

        # Calculate optimal setpoint
        trv_setpoint = self.calculate_trv_setpoint(
            zone_id, room_id, trv_id,
            room_temp, target_temp, trv_internal_temp, needs_heating
        )

        # Send command to TRV
        try:
            # Check if climate domain is available
            if not self.hass.services.has_service("climate", "set_temperature"):
                _LOGGER.debug(
                    "Climate service not yet available, skipping TRV %s update", trv_id
                )
                return

            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": trv_id, "temperature": trv_setpoint},
                blocking=False,
            )

            _LOGGER.info(
                "Set TRV %s: target=%.1f°C, setpoint=%.1f°C "
                "(room=%.1f°C, trv_internal=%.1f°C, overshoot=%.1f°C)",
                trv_id, target_temp, trv_setpoint,
                room_temp if room_temp else 0,
                trv_internal_temp if trv_internal_temp else 0,
                trv_setpoint - target_temp
            )
        except Exception as err:
            # Don't let TRV errors break the entire coordinator update
            _LOGGER.warning("Error setting TRV %s temperature: %s", trv_id, err)
