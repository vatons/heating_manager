"""Schedule management for Heating Manager."""
from datetime import datetime
import logging
import re
from typing import Any

from .const import (
    CONF_END,
    CONF_SCHEDULE,
    CONF_START,
    CONF_TEMPERATURE,
    CONF_WEEKDAY,
    CONF_WEEKEND,
)

_LOGGER = logging.getLogger(__name__)

MINUTES_PER_DAY = 24 * 60
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$")


def parse_time(value: Any) -> int | None:
    """Convert a schedule time to minutes since midnight, or None if invalid.

    Accepts "HH:MM", "H:MM" and "HH:MM:SS" strings, "24:00" for end of day, and
    integers. YAML 1.1 reads an unquoted time such as 21:00 as the base-60
    integer 1260, which is already minutes since midnight.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str):
        match = _TIME_RE.match(value)
        if not match:
            return None
        hours, mins = int(match.group(1)), int(match.group(2))
        if mins > 59:
            return None
        minutes = hours * 60 + mins
    else:
        return None
    if not 0 <= minutes <= MINUTES_PER_DAY:
        return None
    return minutes


def format_time(minutes: int) -> str:
    """Format minutes since midnight as "HH:MM"."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class ScheduleManager:
    """Manages heating schedule parsing and temperature lookups."""

    def __init__(self, minimum_temp: float) -> None:
        """Initialize the schedule manager."""
        self.minimum_temp = minimum_temp
        self._warned_periods: set[str] = set()

    def _day_periods(self, zone_config: dict, day: int) -> list[tuple[int, int, dict]]:
        """Return the valid periods for a weekday (0=Monday) as (start, end, period).

        Periods with missing or unparseable times are skipped with a warning
        (logged once per period) rather than breaking the whole update.
        """
        schedule = zone_config.get(CONF_SCHEDULE) or {}
        if not isinstance(schedule, dict):
            return []
        schedule_key = CONF_WEEKEND if day in (5, 6) else CONF_WEEKDAY
        day_schedule = schedule.get(schedule_key) or []
        if not isinstance(day_schedule, list):
            return []

        periods = []
        for period in day_schedule:
            if not isinstance(period, dict):
                self._warn_invalid(zone_config, period)
                continue
            start = parse_time(period.get(CONF_START))
            end = parse_time(period.get(CONF_END))
            if start is None or end is None:
                self._warn_invalid(zone_config, period)
                continue
            periods.append((start, end, period))
        return periods

    def _warn_invalid(self, zone_config: dict, period: Any) -> None:
        key = f"{zone_config.get('name', 'unknown')}:{period!r}"
        if key not in self._warned_periods:
            self._warned_periods.add(key)
            _LOGGER.warning(
                "Ignoring schedule period with invalid start/end time in zone %s: %s "
                "(use \"HH:MM\")",
                zone_config.get("name", "unknown"),
                period,
            )

    @staticmethod
    def _minutes_of(current_time: datetime) -> int:
        return current_time.hour * 60 + current_time.minute

    def get_scheduled_temperature(self, zone_config: dict, current_time: datetime) -> float:
        """Get the scheduled temperature for the current time."""
        now = self._minutes_of(current_time)

        for start, end, period in self._day_periods(zone_config, current_time.weekday()):
            if self._in_range(start, end, now):
                temp = period.get(CONF_TEMPERATURE, self.minimum_temp)
                _LOGGER.debug(
                    "Scheduled temperature for %s at %s: %.1f°C (in schedule period %s-%s)",
                    zone_config.get("name", "unknown"),
                    format_time(now),
                    temp,
                    format_time(start),
                    format_time(end),
                )
                return temp

        # No active schedule, use minimum temp
        _LOGGER.debug(
            "No active schedule for %s at %s, using minimum temp: %.1f°C",
            zone_config.get("name", "unknown"),
            format_time(now),
            self.minimum_temp,
        )
        return self.minimum_temp

    def get_period_info(
        self, zone_config: dict, current_time: datetime
    ) -> tuple[dict | None, dict | None]:
        """Return (current_period, next_period) for display.

        Each period is {"start", "end", "temperature"} with times as "HH:MM";
        a next period on the following day also has "tomorrow": True.
        """
        now = self._minutes_of(current_time)
        today = self._day_periods(zone_config, current_time.weekday())

        def _info(start: int, end: int, period: dict) -> dict:
            return {
                "start": format_time(start),
                "end": format_time(end),
                "temperature": period.get(CONF_TEMPERATURE),
            }

        current_period = None
        for start, end, period in today:
            if self._in_range(start, end, now):
                current_period = _info(start, end, period)
                break

        upcoming = [p for p in today if p[0] > now and not self._in_range(p[0], p[1], now)]
        if upcoming:
            return current_period, _info(*min(upcoming, key=lambda p: p[0]))

        next_period = None
        if today:
            tomorrow = self._day_periods(zone_config, (current_time.weekday() + 1) % 7)
            if tomorrow:
                next_period = {**_info(*min(tomorrow, key=lambda p: p[0])), "tomorrow": True}
        return current_period, next_period

    def is_time_in_period(self, start: Any, end: Any, current: Any) -> bool:
        """Check whether current is within [start, end); all accept any parse_time() input."""
        start_m, end_m, current_m = parse_time(start), parse_time(end), parse_time(current)
        if start_m is None or end_m is None or current_m is None:
            return False
        return self._in_range(start_m, end_m, current_m)

    @staticmethod
    def _in_range(start: int, end: int, current: int) -> bool:
        """Check if current is within [start, end) in minutes since midnight.

        Handles midnight-spanning periods (e.g. 22:00 - 06:00). A period whose
        start equals its end (e.g. 00:00 - 00:00) covers the whole day.
        """
        if start == end or (start == 0 and end == MINUTES_PER_DAY):
            return True
        if start > end:
            # Period spans midnight: active from start until midnight, and from midnight until end
            return current >= start or current < end
        return start <= current < end
