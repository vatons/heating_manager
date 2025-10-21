"""Schedule management for Heating Manager."""
from datetime import datetime
import logging

from .const import (
    CONF_END,
    CONF_SCHEDULE,
    CONF_START,
    CONF_TEMPERATURE,
    CONF_WEEKDAY,
    CONF_WEEKEND,
)

_LOGGER = logging.getLogger(__name__)


class ScheduleManager:
    """Manages heating schedule parsing and temperature lookups."""

    def __init__(self, minimum_temp: float) -> None:
        """Initialize the schedule manager."""
        self.minimum_temp = minimum_temp

    def get_scheduled_temperature(self, zone_config: dict, current_time: datetime) -> float:
        """Get the scheduled temperature for the current time."""
        schedule = zone_config.get(CONF_SCHEDULE, {})

        # Determine if weekday or weekend
        is_weekend = current_time.weekday() in [5, 6]  # Saturday=5, Sunday=6
        schedule_key = CONF_WEEKEND if is_weekend else CONF_WEEKDAY

        day_schedule = schedule.get(schedule_key, [])
        current_time_str = current_time.strftime("%H:%M")

        for period in day_schedule:
            start = period.get(CONF_START)
            end = period.get(CONF_END)

            if self._time_in_range(start, end, current_time_str):
                temp = period.get(CONF_TEMPERATURE, self.minimum_temp)
                _LOGGER.debug(
                    "Scheduled temperature for %s at %s: %.1f°C (in schedule period %s-%s)",
                    zone_config.get("name", "unknown"),
                    current_time_str,
                    temp,
                    start,
                    end,
                )
                return temp

        # No active schedule, use minimum temp
        _LOGGER.debug(
            "No active schedule for %s at %s, using minimum temp: %.1f°C",
            zone_config.get("name", "unknown"),
            current_time_str,
            self.minimum_temp,
        )
        return self.minimum_temp

    def _time_in_range(self, start: str, end: str, current: str) -> bool:
        """Check if current time is within start and end times."""
        return start <= current < end
