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


def format_period(period: dict) -> str:
    return f"{period[CONF_START]}–{period[CONF_END]}: {period[CONF_TEMPERATURE]:g}°C"


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

    return {OPT_SETTINGS: settings, OPT_ZONES: zones}


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

def options_to_runtime(options: dict) -> tuple[dict, dict]:
    """Return (config, settings) for the coordinator from entry options.

    config has the structure the coordinator and climate platform read:
    {"zones": {...}, "heating_demand_mode": ...}.
    """
    settings = default_settings()
    for key, value in (options.get(OPT_SETTINGS) or {}).items():
        if key in SETTINGS_BY_KEY:
            settings[key] = coerce_setting(SETTINGS_BY_KEY[key], value)
    config = {
        CONF_ZONES: options.get(OPT_ZONES) or {},
        CONF_HEATING_DEMAND_MODE: settings[CONF_HEATING_DEMAND_MODE],
    }
    return config, settings
