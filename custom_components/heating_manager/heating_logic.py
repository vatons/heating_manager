"""Heating logic calculations for Heating Manager."""
import logging

from .const import (
    HEATING_DEMAND_MODE_ANY_ROOM,
    HEATING_DEMAND_MODE_ZONE_AVERAGE,
    MINIMAL_DEADBAND,
    TARGET_REACHED_THRESHOLD,
)

_LOGGER = logging.getLogger(__name__)


class HeatingLogic:
    """Manages heating need calculations and zone demand logic."""

    def __init__(self, heating_deadband: float) -> None:
        """Initialize the heating logic manager."""
        self.heating_deadband = heating_deadband
        self.room_heating_state: dict[str, dict[str, dict]] = {}  # zone_id -> room_id -> {previous_target, target_reached}

    def calculate_heating_need(
        self, zone_id: str, room_id: str, room_temp: float | None, target_temp: float | None
    ) -> bool:
        """Calculate if a room needs heating using smart deadband logic.

        Smart deadband logic:
        - When target temperature changes: Use minimal deadband (0.1°C) to heat immediately
        - When room hasn't reached target yet: Use minimal deadband (0.1°C)
        - When room reached target and is maintaining: Use full configured deadband

        This prevents aggressive cycling while ensuring responsive heating toward new targets.
        """
        if room_temp is None or target_temp is None:
            return False

        # Initialize room heating state if not exists
        if zone_id not in self.room_heating_state:
            self.room_heating_state[zone_id] = {}

        if room_id not in self.room_heating_state[zone_id]:
            self.room_heating_state[zone_id][room_id] = {
                "previous_target": target_temp,
                "target_reached": False,
            }

        room_state = self.room_heating_state[zone_id][room_id]
        previous_target = room_state.get("previous_target")
        target_reached = room_state.get("target_reached", False)

        # Check if target temperature changed
        target_changed = previous_target is None or abs(target_temp - previous_target) > TARGET_REACHED_THRESHOLD

        if target_changed:
            # Target changed - reset state and use minimal deadband
            room_state["previous_target"] = target_temp
            room_state["target_reached"] = False
            target_reached = False
            _LOGGER.debug(
                "Zone %s / Room %s: Target changed from %.1f°C to %.1f°C, using minimal deadband (%.1f°C)",
                zone_id,
                room_id,
                previous_target if previous_target is not None else 0.0,
                target_temp,
                MINIMAL_DEADBAND,
            )

        # Check if room has reached target
        if not target_reached and room_temp >= target_temp - TARGET_REACHED_THRESHOLD:
            room_state["target_reached"] = True
            target_reached = True
            _LOGGER.debug(
                "Zone %s / Room %s: Target %.1f°C reached (room at %.1f°C)",
                zone_id,
                room_id,
                target_temp,
                room_temp,
            )

        # Determine deadband to use
        if not target_reached:
            # Room hasn't reached target yet - use minimal deadband for responsive heating
            deadband = MINIMAL_DEADBAND
            _LOGGER.debug(
                "Zone %s / Room %s: Target not reached yet, using minimal deadband (%.1f°C)",
                zone_id,
                room_id,
                deadband,
            )
        else:
            # Room has reached target - use full configured deadband to prevent cycling
            deadband = self.heating_deadband
            _LOGGER.debug(
                "Zone %s / Room %s: Target reached, using full deadband (%.1f°C)",
                zone_id,
                room_id,
                deadband,
            )

        # Calculate heating need
        needs_heating = room_temp < target_temp - deadband

        _LOGGER.debug(
            "Zone %s / Room %s: temp=%.1f°C, target=%.1f°C, deadband=%.1f°C, needs_heating=%s",
            zone_id,
            room_id,
            room_temp,
            target_temp,
            deadband,
            needs_heating,
        )

        return needs_heating

    def calculate_zone_heating_demand(self, rooms: dict, mode: str) -> bool:
        """Calculate whether a zone requires heating based on the configured mode.

        Args:
            rooms: Dictionary of room data with temperatures and targets
            mode: "any_room" or "zone_average"

        Returns:
            True if zone needs heating, False otherwise
        """
        # BOOST ALWAYS OVERRIDES: If any room has active boost, demand heating
        for room_data in rooms.values():
            if room_data.get("boost"):
                _LOGGER.debug(
                    "Zone heating demand: TRUE (room %s has active boost, overriding mode '%s')",
                    room_data.get("name", "unknown"),
                    mode,
                )
                return True

        if mode == HEATING_DEMAND_MODE_ZONE_AVERAGE:
            # Zone average mode: calculate if average temp < average target
            room_temps = []
            target_temps = []

            for room_data in rooms.values():
                room_temp = room_data.get("temperature")
                target_temp = room_data.get("target_temperature")

                if room_temp is not None and target_temp is not None:
                    room_temps.append(room_temp)
                    target_temps.append(target_temp)

            # If we have valid data, compare averages
            if room_temps and target_temps:
                avg_room_temp = sum(room_temps) / len(room_temps)
                avg_target_temp = sum(target_temps) / len(target_temps)

                return avg_room_temp < avg_target_temp - self.heating_deadband

            return False

        else:  # HEATING_DEMAND_MODE_ANY_ROOM (default)
            # Any room mode: if any room needs heating
            # Note: Boost is already factored into target_temp, so we only check needs_heating
            for room_data in rooms.values():
                if room_data.get("needs_heating"):
                    return True

            return False

    def get_state_for_storage(self) -> dict:
        """Get room heating state for persistence."""
        return self.room_heating_state

    def restore_state(self, stored_state: dict) -> None:
        """Restore room heating state from storage."""
        self.room_heating_state = stored_state
