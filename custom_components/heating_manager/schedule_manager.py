"""Schedule management for Heating Manager."""
from datetime import datetime, timedelta
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


def is_legacy_schedule(schedule: Any) -> bool:
    """Whether a schedule uses the pre-3.7 periods (each with a start and an end)."""
    if not isinstance(schedule, dict):
        return False
    return any(
        isinstance(entry, dict) and CONF_END in entry
        for day in (CONF_WEEKDAY, CONF_WEEKEND)
        for entry in (schedule.get(day) or [])
        if isinstance(schedule.get(day), list)
    )


def _legacy_day(entries: Any) -> list[tuple[int, int, float]]:
    """Valid (start, end, temperature) periods of a pre-3.7 day, in list order."""
    periods = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        start, end = parse_time(entry.get(CONF_START)), parse_time(entry.get(CONF_END))
        try:
            temperature = float(entry.get(CONF_TEMPERATURE))
        except (TypeError, ValueError):
            continue
        if start is not None and end is not None:
            periods.append((start, end, temperature))
    return periods


def _legacy_temperature(periods: list[tuple[int, int, float]], minute: int, outside: float) -> float:
    """What a pre-3.7 day gave at a minute: the first period containing it, else outside."""
    for start, end, temperature in periods:
        if _in_range(start, end, minute):
            return temperature
    return outside


def _legacy_day_changes(periods: list[tuple[int, int, float]], outside: float) -> list[tuple[int, float]]:
    """A pre-3.7 day as changes, starting at 00:00, merging equal neighbours."""
    points = sorted({0} | {s % MINUTES_PER_DAY for s, _, _ in periods} | {e % MINUTES_PER_DAY for _, e, _ in periods})
    changes: list[tuple[int, float]] = []
    for minute in points:
        temperature = _legacy_temperature(periods, minute, outside)
        if not changes or changes[-1][1] != temperature:
            changes.append((minute, temperature))
    return changes


def convert_legacy_schedule(schedule: dict, outside: float) -> dict[str, list[dict]]:
    """Convert pre-3.7 periods (start, end) to temperature changes (start only).

    Before 3.7 each day was looked up on its own: a time not in any period used
    the temperature outside schedule. Now each change lasts until the next one,
    and the last change of a day carries on until the next day's first, also
    from Friday into Saturday and from Sunday into Monday.

    The result gives exactly the same temperature at every minute of the week:
    gaps become changes to the outside temperature, and a day keeps a change at
    00:00 unless the day before (weekday or weekend, whichever it can be) always
    ends at that temperature anyway.
    """
    outside = float(outside)
    days = {
        day: _legacy_day_changes(_legacy_day((schedule or {}).get(day)), outside)
        for day in (CONF_WEEKDAY, CONF_WEEKEND)
    }
    # The day before a weekday is a weekday (Tue-Fri) or the weekend (Mon); before a weekend day,
    # the weekend (Sun) or a weekday (Sat). So each day's 00:00 must match both to be dropped.
    last = {day: changes[-1][1] for day, changes in days.items()}
    result: dict[str, list[dict]] = {}
    for day, changes in days.items():
        if len(changes) == 1 and changes[0][1] == outside:
            result[day] = []            # no changes: the outside temperature all day
            continue
        if len(changes) > 1 and all(changes[0][1] == value for value in last.values()):
            changes = changes[1:]       # midnight carries on from the day before anyway
        result[day] = [
            {CONF_START: format_time(minute), CONF_TEMPERATURE: temperature} for minute, temperature in changes
        ]
    return result


def _in_range(start: int, end: int, current: int) -> bool:
    """Whether current is in a pre-3.7 period [start, end), in minutes since midnight.

    A period ending at or before its start spans midnight; one whose start
    equals its end (e.g. 00:00 - 00:00) covers the whole day.
    """
    if start == end or (start == 0 and end == MINUTES_PER_DAY):
        return True
    if start > end:
        return current >= start or current < end
    return start <= current < end


class ScheduleManager:
    """Works out a zone's scheduled temperature from its temperature changes.

    Each day type (weekday, weekend) is a list of changes: a time and the
    temperature from then on. A change lasts until the next one; the last
    change of a day carries on past midnight until the next day's first, even
    between a weekday and a weekend day (Friday to Saturday, Sunday to Monday).
    A day type without changes uses the temperature outside schedule all day.
    """

    def __init__(self, minimum_temp: float) -> None:
        """Initialize the schedule manager."""
        self.minimum_temp = minimum_temp
        self._warned: set[str] = set()
        self._converted: dict[str, dict] = {}

    def _changes(self, zone_config: dict, day: int) -> list[tuple[int, float]]:
        """A weekday's (0=Monday) valid changes as (minute, temperature), sorted by time."""
        schedule = zone_config.get(CONF_SCHEDULE) or {}
        if not isinstance(schedule, dict):
            return []
        if is_legacy_schedule(schedule):
            # Older configuration (tests, YAML): convert once, exactly
            key = repr((schedule, self.minimum_temp))
            if key not in self._converted:
                for day_type in (CONF_WEEKDAY, CONF_WEEKEND):
                    entries = schedule.get(day_type)
                    for entry in entries if isinstance(entries, list) else []:
                        if not _legacy_day([entry]):
                            self._warn_invalid(zone_config, entry)
                self._converted[key] = convert_legacy_schedule(schedule, self.minimum_temp)
            schedule = self._converted[key]
        entries = schedule.get(CONF_WEEKEND if day in (5, 6) else CONF_WEEKDAY) or []
        changes: dict[int, float] = {}
        for entry in entries if isinstance(entries, list) else []:
            start = parse_time(entry.get(CONF_START)) if isinstance(entry, dict) else None
            try:
                temperature = float(entry.get(CONF_TEMPERATURE)) if isinstance(entry, dict) else None
            except (TypeError, ValueError):
                temperature = None
            if start is None or temperature is None:
                self._warn_invalid(zone_config, entry)
                continue
            changes.setdefault(start % MINUTES_PER_DAY, temperature)
        return sorted(changes.items())

    def _warn_invalid(self, zone_config: dict, entry: Any) -> None:
        key = f"{zone_config.get('name', 'unknown')}:{entry!r}"
        if key not in self._warned:
            self._warned.add(key)
            _LOGGER.warning(
                "Ignoring schedule entry with an invalid time or temperature in zone %s: %s "
                "(use a time like \"07:30\" and a temperature)",
                zone_config.get("name", "unknown"),
                entry,
            )

    def _day(self, zone_config: dict, day: int) -> list[tuple[int, float, bool]]:
        """A day's temperature over time: (minute, temperature, carried over) from 00:00.

        Starts with what carries over from the day before, unless the day has
        no changes (outside temperature all day) or a change at 00:00.
        """
        changes = self._changes(zone_config, day)
        if not changes:
            return [(0, self.minimum_temp, False)]
        timeline = [(minute, temperature, False) for minute, temperature in changes]
        if changes[0][0] > 0:
            before = self._changes(zone_config, (day - 1) % 7)
            carried = before[-1][1] if before else self.minimum_temp
            timeline.insert(0, (0, carried, True))
        return timeline

    @staticmethod
    def _minutes_of(current_time: datetime) -> int:
        return current_time.hour * 60 + current_time.minute

    def get_scheduled_temperature(self, zone_config: dict, current_time: datetime) -> float:
        """The scheduled temperature at a time."""
        now = self._minutes_of(current_time)
        timeline = self._day(zone_config, current_time.weekday())
        temperature = timeline[0][1]
        for minute, value, _ in timeline:
            if minute > now:
                break
            temperature = value
        _LOGGER.debug(
            "Scheduled temperature for %s at %s: %.1f°C",
            zone_config.get("name", "unknown"), format_time(now), temperature,
        )
        return temperature

    def upcoming_changes(
        self, zone_config: dict, current_time: datetime, count: int = 2
    ) -> list[tuple[datetime, float]]:
        """The next `count` changes of temperature after current_time, as (when, temperature),
        within a week. Changes to the temperature already in force aren't changes."""
        changes: list[tuple[datetime, float]] = []
        temperature = self.get_scheduled_temperature(zone_config, current_time)
        now = self._minutes_of(current_time)
        midnight = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(8):
            day = (current_time.weekday() + offset) % 7
            for minute, value, _ in self._day(zone_config, day):
                if offset == 0 and minute <= now:
                    continue
                if value != temperature:
                    changes.append((midnight + timedelta(days=offset, minutes=minute), value))
                    if len(changes) == count:
                        return changes
                    temperature = value
        return changes

    def get_period_info(
        self, zone_config: dict, current_time: datetime
    ) -> tuple[dict | None, dict | None]:
        """Return (current, next) for display.

        current: {"start", "end", "temperature"}: since when this temperature
        applies (with "yesterday": True if it started the day before) and when
        it next changes. next: {"start", "temperature"} of the next change, with
        "tomorrow": True if that's the next day. None when nothing changes.
        """
        day = current_time.weekday()
        now = self._minutes_of(current_time)
        today = self._day(zone_config, day)
        index = max(i for i, (minute, _, _) in enumerate(today) if minute <= now)
        minute, temperature, carried = today[index]
        current: dict[str, Any] = {"start": format_time(minute), "end": None, "temperature": temperature}
        if carried:
            before = self._changes(zone_config, (day - 1) % 7)
            if before:
                current["start"] = format_time(before[-1][0])
                current["yesterday"] = True

        upcoming = None
        for next_minute, value, _ in today[index + 1:]:
            if value != temperature:
                upcoming = {"start": format_time(next_minute), "temperature": value}
                break
        if upcoming is None:
            for next_minute, value, _ in self._day(zone_config, (day + 1) % 7):
                if value != temperature:
                    upcoming = {"start": format_time(next_minute), "temperature": value, "tomorrow": True}
                    break
        if upcoming is not None:
            current["end"] = upcoming["start"]
        if not self._changes(zone_config, day) and not self._changes(zone_config, (day - 1) % 7):
            current = None if upcoming is None else current
        return current, upcoming
