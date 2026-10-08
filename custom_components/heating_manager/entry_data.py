"""Config entry data model for Heating Manager.

Everything the integration needs is stored in the config entry's options:

    {
        "settings": {<setting key>: value, ...},
        "zones": {
            zone_id: {
                "name": str,
                "heating_demand_mode": str,          # optional per-zone override
                "monitoring_only": bool,             # optional: report only, never heat
                "schedule": {"weekday": [period, ...], "weekend": [period, ...]},
                "rooms": {
                    room_id: {
                        "name": str,
                        "trvs": [climate entity_id, ...],
                        "sensors": [{"temperature": entity_id, "last_seen": entity_id?}, ...],
                        "temperature_offset": float,  # optional
                    },
                },
            },
        },
    }

where a period is {"start": "HH:MM", "end": "HH:MM", "temperature": float}.
Zone and room ids are kept stable (they form the entities' unique ids).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.util import slugify

from .const import (
    CONF_ANALYTICS_ENABLED,
    CONF_ANALYTICS_HISTORY_SIZE,
    CONF_ANALYTICS_MIN_SAMPLES,
    CONF_BOOST_DURATION,
    CONF_DERIVATIVE_SMOOTHING,
    CONF_END,
    CONF_FALLBACK_MODE,
    CONF_FROST_PROTECTION_TEMP,
    CONF_HEATING_DEADBAND,
    CONF_HEATING_DEMAND_MODE,
    CONF_MIN_BOILER_OFF_TIME,
    CONF_MIN_BOILER_ON_TIME,
    CONF_MINIMUM_TEMP,
    CONF_MONITORING_ONLY,
    CONF_ROOMS,
    CONF_SCHEDULE,
    CONF_SENSORS,
    CONF_START,
    CONF_TEMPERATURE,
    CONF_TEMPERATURE_OFFSET,
    CONF_TRV_COOLDOWN_OFFSET,
    CONF_TRV_OFFSET_EMA_ALPHA,
    CONF_TRV_OVERSHOOT_ENABLED,
    CONF_TRV_OVERSHOOT_MAX,
    CONF_TRV_OVERSHOOT_THRESHOLD,
    CONF_TRVS,
    CONF_UPDATE_INTERVAL,
    CONF_WEEKDAY,
    CONF_WEEKEND,
    CONF_ZONES,
    DEFAULT_ANALYTICS_ENABLED,
    DEFAULT_ANALYTICS_HISTORY_SIZE,
    DEFAULT_ANALYTICS_MIN_SAMPLES,
    DEFAULT_BOOST_DURATION,
    DEFAULT_DERIVATIVE_SMOOTHING,
    DEFAULT_FALLBACK_MODE,
    DEFAULT_FROST_PROTECTION_TEMP,
    DEFAULT_HEATING_DEADBAND,
    DEFAULT_HEATING_DEMAND_MODE,
    DEFAULT_MIN_BOILER_OFF_TIME,
    DEFAULT_MIN_BOILER_ON_TIME,
    DEFAULT_MINIMUM_TEMP,
    DEFAULT_TRV_COOLDOWN_OFFSET,
    DEFAULT_TRV_OFFSET_EMA_ALPHA,
    DEFAULT_TRV_OVERSHOOT_ENABLED,
    DEFAULT_TRV_OVERSHOOT_MAX,
    DEFAULT_TRV_OVERSHOOT_THRESHOLD,
    DEFAULT_UPDATE_INTERVAL,
    FALLBACK_MODE_LAST_KNOWN,
    FALLBACK_MODE_TRV,
    FALLBACK_MODE_ZONE_AVERAGE,
    HEATING_DEMAND_MODE_ANY_ROOM,
    HEATING_DEMAND_MODE_ZONE_AVERAGE,
)
from .schedule_manager import format_time, parse_time

_LOGGER = logging.getLogger(__name__)

OPT_SETTINGS = "settings"
OPT_ZONES = CONF_ZONES

DAY_TYPES = (CONF_WEEKDAY, CONF_WEEKEND)

HEATING_DEMAND_MODES = [HEATING_DEMAND_MODE_ANY_ROOM, HEATING_DEMAND_MODE_ZONE_AVERAGE]
FALLBACK_MODES = [FALLBACK_MODE_ZONE_AVERAGE, FALLBACK_MODE_TRV, FALLBACK_MODE_LAST_KNOWN]


@dataclass(frozen=True)
class SettingSpec:
    """A global setting: its key, default, kind and allowed range."""

    key: str
    default: Any
    kind: str                      # "float", "int", "bool" or "select"
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str | None = None
    options: tuple[str, ...] = ()


SETTINGS: tuple[SettingSpec, ...] = (
    # Basic
    SettingSpec(CONF_MINIMUM_TEMP, DEFAULT_MINIMUM_TEMP, "float", minimum=5, maximum=30, step=0.5, unit="°C"),
    SettingSpec(CONF_FROST_PROTECTION_TEMP, DEFAULT_FROST_PROTECTION_TEMP, "float", minimum=1, maximum=15, step=0.5, unit="°C"),
    SettingSpec(CONF_HEATING_DEMAND_MODE, DEFAULT_HEATING_DEMAND_MODE, "select", options=tuple(HEATING_DEMAND_MODES)),
    SettingSpec(CONF_FALLBACK_MODE, DEFAULT_FALLBACK_MODE, "select", options=tuple(FALLBACK_MODES)),
    SettingSpec(CONF_HEATING_DEADBAND, DEFAULT_HEATING_DEADBAND, "float", minimum=0.1, maximum=5, step=0.1, unit="°C"),
    SettingSpec(CONF_BOOST_DURATION, DEFAULT_BOOST_DURATION, "int", minimum=1, maximum=480, step=1, unit="min"),
    # Advanced
    SettingSpec(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, "int", True, 10, 600, 1, "s"),
    SettingSpec(CONF_MIN_BOILER_ON_TIME, DEFAULT_MIN_BOILER_ON_TIME, "int", True, 0, 30, 1, "min"),
    SettingSpec(CONF_MIN_BOILER_OFF_TIME, DEFAULT_MIN_BOILER_OFF_TIME, "int", True, 0, 30, 1, "min"),
    SettingSpec(CONF_TRV_OVERSHOOT_ENABLED, DEFAULT_TRV_OVERSHOOT_ENABLED, "bool", True),
    SettingSpec(CONF_TRV_OVERSHOOT_MAX, DEFAULT_TRV_OVERSHOOT_MAX, "float", True, 0, 10, 0.5, "°C"),
    SettingSpec(CONF_TRV_OVERSHOOT_THRESHOLD, DEFAULT_TRV_OVERSHOOT_THRESHOLD, "float", True, 0, 3, 0.1, "°C"),
    SettingSpec(CONF_TRV_COOLDOWN_OFFSET, DEFAULT_TRV_COOLDOWN_OFFSET, "float", True, 0, 5, 0.5, "°C"),
    SettingSpec(CONF_TRV_OFFSET_EMA_ALPHA, DEFAULT_TRV_OFFSET_EMA_ALPHA, "float", True, 0.01, 1, 0.01),
    SettingSpec(CONF_ANALYTICS_ENABLED, DEFAULT_ANALYTICS_ENABLED, "bool", True),
    SettingSpec(CONF_ANALYTICS_HISTORY_SIZE, DEFAULT_ANALYTICS_HISTORY_SIZE, "int", True, 5, 500, 1),
    SettingSpec(CONF_ANALYTICS_MIN_SAMPLES, DEFAULT_ANALYTICS_MIN_SAMPLES, "int", True, 2, 50, 1),
    SettingSpec(CONF_DERIVATIVE_SMOOTHING, DEFAULT_DERIVATIVE_SMOOTHING, "float", True, 0.01, 1, 0.01),
)
SETTINGS_BY_KEY = {spec.key: spec for spec in SETTINGS}

DEFAULT_SCHEDULE_PERIOD = {CONF_START: "06:30", CONF_END: "22:00", CONF_TEMPERATURE: 19.0}


def default_settings() -> dict[str, Any]:
    return {spec.key: spec.default for spec in SETTINGS}


def coerce_setting(spec: SettingSpec, value: Any) -> Any:
    """Coerce and range-check one setting; fall back to its default if invalid."""
    try:
        if spec.kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if spec.kind == "select":
            return value if value in spec.options else spec.default
        number = int(value) if spec.kind == "int" else float(value)
    except (TypeError, ValueError):
        _LOGGER.warning("Invalid value %r for %s, using default %r", value, spec.key, spec.default)
        return spec.default
    if (spec.minimum is not None and number < spec.minimum) or (
        spec.maximum is not None and number > spec.maximum
    ):
        _LOGGER.warning(
            "%s=%s is outside %s-%s, using default %r",
            spec.key, number, spec.minimum, spec.maximum, spec.default,
        )
        return spec.default
    return number


def unique_id_for(name: str, existing: set[str] | dict, fallback: str) -> str:
    """Slugify a display name into an id not already in existing."""
    base = slugify(name)
    # slugify() returns "unknown" when nothing usable is left (e.g. "!!!")
    if not base or (base == "unknown" and "unknown" not in name.lower()):
        base = fallback
    candidate, n = base, 2
    while candidate in existing:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def new_zone(name: str) -> dict[str, Any]:
    return {
        "name": name,
        CONF_SCHEDULE: {day: [dict(DEFAULT_SCHEDULE_PERIOD)] for day in DAY_TYPES},
        CONF_ROOMS: {},
    }


def new_options(first_zone_name: str | None = None) -> dict[str, Any]:
    zones = {}
    if first_zone_name:
        zones[unique_id_for(first_zone_name, {}, "zone_1")] = new_zone(first_zone_name)
    return {OPT_SETTINGS: default_settings(), OPT_ZONES: zones}


def period_minutes(period: dict) -> set[int]:
    """Minutes of the day a period covers (handles midnight wrap and all-day)."""
    start, end = parse_time(period.get(CONF_START)), parse_time(period.get(CONF_END))
    if start is None or end is None:
        return set()
    start, end = start % (24 * 60), end % (24 * 60)
    if start == end:
        return set(range(24 * 60))
    if start < end:
        return set(range(start, end))
    return set(range(start, 24 * 60)) | set(range(0, end))


def sort_periods(periods: list[dict]) -> list[dict]:
    return sorted(periods, key=lambda p: parse_time(p.get(CONF_START)) or 0)


def first_overlap(periods: list[dict]) -> str | None:
    """"HH:MM–HH:MM and HH:MM–HH:MM" for the first two periods that overlap, else None."""
    for i, first in enumerate(periods):
        for second in periods[i + 1:]:
            if period_minutes(first) & period_minutes(second):
                return f"{first[CONF_START]}–{first[CONF_END]} and {second[CONF_START]}–{second[CONF_END]}"
    return None


def schedule_overlaps(zone_name: str, schedule: dict) -> list[str]:
    """Describe overlapping periods in a zone's schedule, one line per day type."""
    problems = []
    for day, label in ((CONF_WEEKDAY, "weekdays"), (CONF_WEEKEND, "weekends")):
        if overlap := first_overlap((schedule or {}).get(day) or []):
            problems.append(f"{zone_name} {label}: {overlap}")
    return problems


def format_period(period: dict) -> str:
    return f"{period[CONF_START]}–{period[CONF_END]}: {period[CONF_TEMPERATURE]:g}°C"


# ---------------------------------------------------------------------------
# TRVs shared between rooms
# ---------------------------------------------------------------------------
# A TRV in two rooms gets two conflicting setpoints every update and flips
# between them, so each TRV belongs to the first room (in config order) only.

def trv_owner(zones: dict, trv_id: str, exclude: tuple[str, str] | None = None) -> tuple[str, str] | None:
    """(zone_id, room_id) of the room that has trv_id, ignoring exclude."""
    for zone_id, zone in zones.items():
        rooms = zone.get(CONF_ROOMS) if isinstance(zone, dict) else None
        if not isinstance(rooms, dict):
            continue
        for room_id, room in rooms.items():
            if (zone_id, room_id) != exclude and isinstance(room, dict) and trv_id in (room.get(CONF_TRVS) or []):
                return zone_id, room_id
    return None


def room_label(zones: dict, zone_id: str, room_id: str) -> str:
    zone = zones.get(zone_id, {})
    room = zone.get(CONF_ROOMS, {}).get(room_id, {})
    return f"{zone.get('name', zone_id)} / {room.get('name', room_id)}"


def dedupe_trvs(zones: dict) -> list[str]:
    """Remove TRVs already used by an earlier room (mutates zones). Returns descriptions."""
    seen: dict[str, tuple[str, str]] = {}
    removed = []
    for zone_id, zone in zones.items():
        rooms = zone.get(CONF_ROOMS) if isinstance(zone, dict) else None
        if not isinstance(rooms, dict):
            continue
        for room_id, room in rooms.items():
            if not isinstance(room, dict):
                continue
            kept = []
            for trv_id in room.get(CONF_TRVS) or []:
                if trv_id in seen:
                    removed.append(
                        f"{trv_id} is already in {room_label(zones, *seen[trv_id])}; "
                        f"not used for {room_label(zones, zone_id, room_id)}"
                    )
                else:
                    seen[trv_id] = (zone_id, room_id)
                    kept.append(trv_id)
            if CONF_TRVS in room:
                room[CONF_TRVS] = kept
    return removed


# ---------------------------------------------------------------------------
# Entity unique ids
# ---------------------------------------------------------------------------
# Prefixed and ':'-separated so ids can't run together. Room ids are unique
# across all zones, so a room keeps its entity when it moves to another zone.

GLOBAL_UNIQUE_ID = "heating_manager:global"


def zone_unique_id(zone_id: str) -> str:
    return f"heating_manager:zone:{zone_id}"


def room_unique_id(room_id: str) -> str:
    return f"heating_manager:room:{room_id}"


def legacy_unique_id_map(zones: dict) -> dict[str, str]:
    """Map older unique ids to current ones, from runtime zones (entry_to_runtime).

    Covers 2.0/2.1 ("heating_manager_{zone}_{room}", which could collide; the
    first room in config order wins, as that's the entity that was created)
    and 2.2 ("heating_manager:room:{zone}:{room}").
    """
    mapping = {"heating_manager_global": GLOBAL_UNIQUE_ID}
    for zone_id, zone in zones.items():
        for room_id, room in (zone.get(CONF_ROOMS) or {}).items():
            old_room_id = room.get("previous_room_id") or room_id
            old_zone_id = room.get("previous_zone_id") or zone_id
            mapping.setdefault(f"heating_manager_{old_zone_id}_{old_room_id}", room_unique_id(room_id))
            mapping.setdefault(f"heating_manager:room:{old_zone_id}:{old_room_id}", room_unique_id(room_id))
        mapping.setdefault(f"heating_manager_{zone_id}_zone", zone_unique_id(zone_id))
    return mapping


# ---------------------------------------------------------------------------
# Subentries: one per zone, holding its rooms
# ---------------------------------------------------------------------------
# Each zone is a subentry whose data includes its rooms, so the integration
# page shows every zone as one card with its room devices inside it.

SUBENTRY_ZONE = "zone"
SUBENTRY_ROOM = "room"          # 3.0/3.1 only: rooms were subentries of their own
CONF_ZONE_ID = "zone_id"
CONF_ROOM_ID = "room_id"
CONF_WEEKEND_SAME = "weekend_same_as_weekday"
# Kept on a room after a migration so its old unique ids still map to it
ROOM_HISTORY_KEYS = ("previous_room_id", "previous_zone_id")


def _count(number: int, singular: str, plural: str) -> str:
    return f"{number} {singular if number == 1 else plural}"


def room_label_for(room_name: str, zone_name: str) -> str:
    """"Downstairs › Lounge": names a room in messages."""
    return f"{zone_name} › {room_name}"


def zone_title(zone: dict) -> str:
    """"Downstairs · 3 rooms" (plus "monitoring only")."""
    rooms = len(zone.get(CONF_ROOMS) or [])
    parts = [zone.get("name", ""), _count(rooms, "room", "rooms") if rooms else "no rooms"]
    if zone.get(CONF_MONITORING_ONLY):
        parts.append("monitoring only")
    return " · ".join(parts)


def stored_room(room_id: str, room: dict, **extra) -> dict[str, Any]:
    """A room as stored in its zone's subentry data."""
    data: dict[str, Any] = {
        CONF_ROOM_ID: room_id,
        "name": room.get("name", room_id),
        CONF_TRVS: list(room.get(CONF_TRVS) or []),
        CONF_SENSORS: [dict(s) for s in room.get(CONF_SENSORS) or []],
    }
    if room.get(CONF_TEMPERATURE_OFFSET):
        data[CONF_TEMPERATURE_OFFSET] = float(room[CONF_TEMPERATURE_OFFSET])
    for key in ROOM_HISTORY_KEYS:
        if value := extra.get(key, room.get(key)):
            data[key] = value
    return data


def zone_subentry(zone_id: str, zone: dict, rooms: list[dict] | None = None) -> dict[str, Any]:
    """ConfigSubentryData for a zone (from the options/YAML zone structure)."""
    schedule = zone.get(CONF_SCHEDULE) or {}
    weekday = list(schedule.get(CONF_WEEKDAY) or [])
    weekend = list(schedule.get(CONF_WEEKEND) or [])
    data: dict[str, Any] = {
        CONF_ZONE_ID: zone_id,
        "name": zone.get("name", zone_id),
        CONF_SCHEDULE: {CONF_WEEKDAY: weekday, CONF_WEEKEND: weekend},
        CONF_WEEKEND_SAME: weekday == weekend,
        CONF_ROOMS: list(rooms or []),
    }
    if zone.get(CONF_HEATING_DEMAND_MODE) in HEATING_DEMAND_MODES:
        data[CONF_HEATING_DEMAND_MODE] = zone[CONF_HEATING_DEMAND_MODE]
    if zone.get(CONF_MONITORING_ONLY):
        data[CONF_MONITORING_ONLY] = True
    return {"subentry_type": SUBENTRY_ZONE, "title": zone_title(data), "unique_id": zone_id, "data": data}


def zones_to_subentries(zones: dict, taken_room_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Convert the options/YAML zones structure to zone subentry data.

    Room ids must be unique across zones; a clash (e.g. "bathroom" in two
    zones) gets a zone-prefixed id, remembering the old one so its entity
    keeps its entity id. TRVs listed in more than one room stay in the first.
    """
    zones = copy.deepcopy(zones)
    for problem in dedupe_trvs(zones):
        _LOGGER.warning("TRV listed in more than one room: %s", problem)
    taken = set(taken_room_ids or ())
    result = []
    for zone_id, zone in zones.items():
        if not isinstance(zone, dict):
            continue
        rooms = []
        for room_id, room in (zone.get(CONF_ROOMS) or {}).items():
            new_id = room_id
            if new_id in taken:
                new_id = unique_id_for(f"{zone_id}_{room_id}", taken, "room")
            taken.add(new_id)
            rooms.append(stored_room(
                new_id, room,
                previous_room_id=room_id if new_id != room_id else None,
                previous_zone_id=zone_id,
            ))
        result.append(zone_subentry(zone_id, zone, rooms))
    return result


def entry_to_runtime(entry: Any) -> tuple[dict, dict]:
    """Return (config, settings) for the coordinator from a config entry.

    config has the structure the coordinator and climate platform read:
    {"zones": {zone_id: {..., "rooms": {room_id: {...}}}}, "heating_demand_mode": ...}.
    Each zone carries its "subentry_id".
    """
    settings = default_settings()
    for key, value in (entry.options.get(OPT_SETTINGS) or {}).items():
        if key in SETTINGS_BY_KEY:
            settings[key] = coerce_setting(SETTINGS_BY_KEY[key], value)

    zones: dict[str, dict] = {}
    for sub in entry.subentries.values():
        if sub.subentry_type != SUBENTRY_ZONE:
            continue
        data = copy.deepcopy(dict(sub.data))
        schedule = dict(data.get(CONF_SCHEDULE) or {})
        if data.get(CONF_WEEKEND_SAME):
            schedule[CONF_WEEKEND] = copy.deepcopy(schedule.get(CONF_WEEKDAY, []))
        zone = {
            "name": data.get("name", sub.title),
            CONF_SCHEDULE: schedule,
            CONF_ROOMS: {},
            "subentry_id": sub.subentry_id,
        }
        for key in (CONF_HEATING_DEMAND_MODE, CONF_MONITORING_ONLY):
            if data.get(key):
                zone[key] = data[key]
        for room in data.get(CONF_ROOMS) or []:
            room = dict(room)
            zone[CONF_ROOMS][room.pop(CONF_ROOM_ID)] = room
        zones[data[CONF_ZONE_ID]] = zone
    for problem in dedupe_trvs(zones):
        _LOGGER.warning("TRV assigned to more than one room: %s", problem)
    return {CONF_ZONES: zones, CONF_HEATING_DEMAND_MODE: settings[CONF_HEATING_DEMAND_MODE]}, settings


def all_room_ids(entry: Any, exclude_zone: str | None = None) -> set[str]:
    """Room ids in use (room ids are unique across zones)."""
    return {
        room.get(CONF_ROOM_ID)
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_ZONE and sub.data.get(CONF_ZONE_ID) != exclude_zone
        for room in sub.data.get(CONF_ROOMS) or []
    }


def match_room_ids(new_rooms: list[dict], old_rooms: list[dict], taken: set[str]) -> list[dict]:
    """Give each room from the zone form the id (and history) of the room it was.

    The form can't carry ids, so a room keeps its id, and so its entity, if
    it has the same id (YAML import), else the same name, else the same TRVs
    and sensors (it was renamed), else the same position in an
    unchanged-length list. Otherwise it's new.
    """
    unused = list(old_rooms)
    taken = set(taken)

    def take(predicate) -> dict | None:
        for old in unused:
            if predicate(old):
                unused.remove(old)
                return old
        return None

    def devices(room: dict) -> tuple:
        sensors = [s["temperature"] if isinstance(s, dict) else s for s in room.get(CONF_SENSORS) or []]
        return (sorted(room.get(CONF_TRVS) or []), sorted(sensors))

    matched: list[dict | None] = [
        take(lambda old, room_id=room.get(CONF_ROOM_ID): old[CONF_ROOM_ID] == room_id) if room.get(CONF_ROOM_ID) else None
        for room in new_rooms
    ]
    for index, room in enumerate(new_rooms):
        if matched[index] is None:
            name = room["name"].casefold()
            matched[index] = take(lambda old, name=name: old["name"].casefold() == name)
    for index, room in enumerate(new_rooms):
        if matched[index] is None and any(devices(room)):
            matched[index] = take(lambda old, room=room: devices(old) == devices(room))
    if len(new_rooms) == len(old_rooms):
        for index in range(len(new_rooms)):
            if matched[index] is None and old_rooms[index] in unused:
                unused.remove(old_rooms[index])
                matched[index] = old_rooms[index]

    taken.update(old[CONF_ROOM_ID] for old in old_rooms)
    result = []
    for room, old in zip(new_rooms, matched):
        if old is not None:
            result.append(stored_room(old[CONF_ROOM_ID], {**room, **{k: old[k] for k in ROOM_HISTORY_KEYS if k in old}}))
        else:
            room_id = unique_id_for(room.get(CONF_ROOM_ID) or room["name"], taken, "room")
            taken.add(room_id)
            result.append(stored_room(room_id, room))
    return result


# ---------------------------------------------------------------------------
# YAML import
# ---------------------------------------------------------------------------

def _normalise_schedule(zone_name: str, schedule: Any) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {day: [] for day in DAY_TYPES}
    if not isinstance(schedule, dict):
        return result
    for day in DAY_TYPES:
        for period in schedule.get(day) or []:
            if not isinstance(period, dict):
                continue
            start, end = parse_time(period.get(CONF_START)), parse_time(period.get(CONF_END))
            try:
                temperature = float(period.get(CONF_TEMPERATURE))
            except (TypeError, ValueError):
                temperature = None
            if start is None or end is None or temperature is None:
                _LOGGER.warning("Not importing invalid schedule period in %s: %s", zone_name, period)
                continue
            # "24:00" becomes "00:00": an end of midnight (00:00-24:00 is all day either way)
            result[day].append({
                CONF_START: format_time(start % (24 * 60)),
                CONF_END: format_time(end % (24 * 60)),
                CONF_TEMPERATURE: temperature,
            })
        result[day] = sort_periods(result[day])
    return result


def _normalise_sensors(sensors: Any) -> list[dict]:
    result = []
    for sensor in sensors or []:
        if isinstance(sensor, str):
            result.append({"temperature": sensor})
        elif isinstance(sensor, dict) and sensor.get("temperature"):
            entry = {"temperature": sensor["temperature"]}
            if sensor.get("last_seen"):
                entry["last_seen"] = sensor["last_seen"]
            result.append(entry)
    return result


def yaml_to_options(heating_config: dict, conf: dict | None = None) -> dict[str, Any]:
    """Convert heating_manager.yaml (plus configuration.yaml overrides) to entry options.

    Settings precedence matches the old YAML setup: heating_manager.yaml, then
    configuration.yaml, then defaults.
    """
    conf = conf or {}
    heating_config = heating_config if isinstance(heating_config, dict) else {}

    settings = {}
    for spec in SETTINGS:
        value = heating_config.get(spec.key, conf.get(spec.key, spec.default))
        settings[spec.key] = coerce_setting(spec, value)

    zones: dict[str, dict] = {}
    for zone_id, zone in (heating_config.get(CONF_ZONES) or {}).items():
        if not isinstance(zone, dict):
            _LOGGER.warning("Not importing zone %s: not a mapping", zone_id)
            continue
        zone_id = str(zone_id)
        name = str(zone.get("name") or zone_id)
        zone_options: dict[str, Any] = {
            "name": name,
            CONF_SCHEDULE: _normalise_schedule(name, zone.get(CONF_SCHEDULE)),
            CONF_ROOMS: {},
        }
        if zone.get(CONF_HEATING_DEMAND_MODE) in HEATING_DEMAND_MODES:
            zone_options[CONF_HEATING_DEMAND_MODE] = zone[CONF_HEATING_DEMAND_MODE]
        if zone.get(CONF_MONITORING_ONLY) is True:
            zone_options[CONF_MONITORING_ONLY] = True
        rooms = zone.get(CONF_ROOMS) or {}
        if isinstance(rooms, dict):
            for room_id, room in rooms.items():
                if not isinstance(room, dict):
                    _LOGGER.warning("Not importing room %s/%s: not a mapping", zone_id, room_id)
                    continue
                room_options: dict[str, Any] = {
                    "name": str(room.get("name") or room_id),
                    CONF_TRVS: [t for t in (room.get(CONF_TRVS) or []) if isinstance(t, str)],
                    CONF_SENSORS: _normalise_sensors(room.get(CONF_SENSORS)),
                }
                try:
                    offset = float(room.get(CONF_TEMPERATURE_OFFSET) or 0.0)
                except (TypeError, ValueError):
                    offset = 0.0
                if offset:
                    room_options[CONF_TEMPERATURE_OFFSET] = offset
                zone_options[CONF_ROOMS][str(room_id)] = room_options
        zones[zone_id] = zone_options

    for problem in dedupe_trvs(zones):
        _LOGGER.warning("Importing: TRV listed in more than one room: %s", problem)
    for zone in zones.values():
        for problem in schedule_overlaps(zone["name"], zone[CONF_SCHEDULE]):
            _LOGGER.warning("Importing: schedule periods overlap (%s); fix them in the zone's form", problem)
    return {OPT_SETTINGS: settings, OPT_ZONES: zones}


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
