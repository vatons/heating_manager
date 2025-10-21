"""Temperature validation for Heating Manager."""
import logging

from .const import MIN_VALID_TEMP, MAX_VALID_TEMP

_LOGGER = logging.getLogger(__name__)


class TemperatureValidator:
    """Validates temperature readings for physical plausibility."""

    def __init__(self, max_change_per_min: float):
        """Initialize the temperature validator.

        Args:
            max_change_per_min: Maximum plausible temperature change per minute in °C
        """
        self.max_change_per_min = max_change_per_min

    def is_plausible_change(
        self, current: float, previous: float | None, time_delta_seconds: float
    ) -> bool:
        """Check if temperature change is physically plausible.

        Args:
            current: Current temperature reading
            previous: Previous temperature reading (None if first reading)
            time_delta_seconds: Time elapsed since previous reading in seconds

        Returns:
            True if change is plausible, False otherwise
        """
        if previous is None:
            # First reading, no comparison possible
            return True

        if time_delta_seconds <= 0:
            # Invalid time delta
            _LOGGER.warning(
                "Invalid time delta for plausibility check: %s seconds",
                time_delta_seconds,
            )
            return False

        time_delta_minutes = time_delta_seconds / 60.0
        max_change = self.max_change_per_min * time_delta_minutes
        actual_change = abs(current - previous)

        if actual_change > max_change:
            _LOGGER.warning(
                "Temperature change of %.2f°C in %.1f minutes exceeds maximum plausible change of %.2f°C (%.2f°C/min)",
                actual_change,
                time_delta_minutes,
                max_change,
                self.max_change_per_min,
            )
            return False

        return True

    def is_in_valid_range(self, temp: float) -> bool:
        """Check if temperature is in valid range.

        Args:
            temp: Temperature to validate

        Returns:
            True if temperature is in valid range, False otherwise
        """
        if not (MIN_VALID_TEMP <= temp <= MAX_VALID_TEMP):
            _LOGGER.warning(
                "Temperature %.1f°C is outside valid range (%.1f°C to %.1f°C)",
                temp,
                MIN_VALID_TEMP,
                MAX_VALID_TEMP,
            )
            return False

        return True

    def validate(
        self, current: float, previous: float | None = None, time_delta_seconds: float | None = None
    ) -> bool:
        """Perform full validation on a temperature reading.

        Args:
            current: Current temperature reading
            previous: Previous temperature reading (optional)
            time_delta_seconds: Time elapsed since previous reading (optional)

        Returns:
            True if temperature passes all validation checks, False otherwise
        """
        # Check if in valid range
        if not self.is_in_valid_range(current):
            return False

        # Check plausibility of change if previous reading available
        if previous is not None and time_delta_seconds is not None:
            if not self.is_plausible_change(current, previous, time_delta_seconds):
                return False

        return True
