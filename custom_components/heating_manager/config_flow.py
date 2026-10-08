"""Config flow, zone/room subentry flows and options flow for Heating Manager.

- Add Integration: name the first zone; creates the entry and that zone.
- Zones and rooms are subentries: added, reconfigured and deleted from the
  integration page, each with a single form. Changes apply immediately.
- Configure (options): Settings (one form in sections) and Import from YAML file.
"""
from __future__ import annotations

import copy
import os
from typing import Any

import voluptuous as vol
import yaml

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .const import (
    CONF_END,
    CONF_HEATING_DEMAND_MODE,
    CONF_MONITORING_ONLY,
    CONF_ROOMS,
    CONF_SCHEDULE,
    CONF_SENSORS,
    CONF_START,
    CONF_TEMPERATURE,
    CONF_TEMPERATURE_OFFSET,
    CONF_TRVS,
    CONF_WEEKDAY,
    CONF_WEEKEND,
    DOMAIN,
)
from .entry_data import (
    CONF_ROOM_ID,
    CONF_WEEKEND_SAME,
    CONF_ZONE_ID,
    DEFAULT_SCHEDULE_PERIOD,
    HEATING_DEMAND_MODES,
    OPT_SETTINGS,
    OPT_ZONES,
    SETTINGS_BY_KEY,
    SUBENTRY_ZONE,
    SettingSpec,
    all_room_ids,
    coerce_setting,
    dedupe_trvs,
    default_settings,
    entry_to_runtime,
    first_overlap,
    match_room_ids,
    room_label_for,
    schedule_overlaps,
    sort_periods,
    unique_id_for,
    zone_title,
    yaml_to_options,
    zone_subentry,
    zones_to_subentries,
)
from .schedule_manager import format_time, parse_time

TITLE = "Heating Manager"
GLOBAL = "global"

CONF_NAME = "name"
CONF_ZONE = "zone"
CONF_PERIODS = "periods"
CONF_SAME = "same_as_weekdays"
CONF_ADVANCED = "advanced"
CONF_LAST_SEEN = "last_seen"
CONF_CONFIRM = "confirm"
CONF_PATH = "path"
CONF_IMPORT_SETTINGS = "import_settings"
DEFAULT_IMPORT_PATH = "heating_manager.yaml"

# Settings form: (section, setting keys, collapsed)
SETTINGS_SECTIONS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("temperatures", ("minimum_temp", "frost_protection_temp"), False),
    ("heating_demand", ("heating_demand_mode", "heating_deadband"), False),
    ("boiler", ("min_boiler_on_time", "min_boiler_off_time"), False),
    ("sensors", ("fallback_mode",), False),
    ("boost", ("boost_duration",), False),
    ("trv_control", (
        "trv_overshoot_enabled", "trv_overshoot_max", "trv_overshoot_threshold",
        "trv_cooldown_offset", "trv_offset_ema_alpha",
    ), True),
    ("analytics", (
        "analytics_enabled", "analytics_history_size", "analytics_min_samples",
        "derivative_smoothing_factor",
    ), True),
    ("updates", ("update_interval",), True),
)


def _setting_selector(spec: SettingSpec) -> selector.Selector:
    if spec.kind == "bool":
        return selector.BooleanSelector()
    if spec.kind == "select":
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(spec.options),
                mode=selector.SelectSelectorMode.DROPDOWN,
                translation_key=spec.key,
            )
        )
    config = selector.NumberSelectorConfig(
        min=spec.minimum, max=spec.maximum, step=spec.step, mode=selector.NumberSelectorMode.BOX
    )
    if spec.unit:
        config["unit_of_measurement"] = spec.unit
    return selector.NumberSelector(config)


# A day's schedule: an editable list of periods, each edited in its own dialog
PERIODS_SELECTOR = selector.ObjectSelector(
    selector.ObjectSelectorConfig(
        multiple=True,
        label_field=CONF_START,
        description_field=CONF_TEMPERATURE,
        translation_key="schedule_period",
        fields={
            CONF_START: {"required": True, "selector": {"time": {}}},
            CONF_END: {"required": True, "selector": {"time": {}}},
            CONF_TEMPERATURE: {
                "required": True,
                "selector": {"number": {
                    "min": 5, "max": 30, "step": 0.5, "unit_of_measurement": "°C", "mode": "box",
                }},
            },
        },
    )
)

# A zone's rooms: an editable list, each room edited in its own dialog
ROOMS_SELECTOR = selector.ObjectSelector(
    selector.ObjectSelectorConfig(
        multiple=True,
        label_field=CONF_NAME,
        description_field=CONF_SENSORS,
        translation_key="zone_room",
        fields={
            CONF_NAME: {"required": True, "selector": {"text": {}}},
            CONF_TRVS: {"required": False, "selector": {"entity": {"domain": "climate", "multiple": True}}},
            CONF_SENSORS: {"required": False, "selector": {"entity": {
                "domain": "sensor", "device_class": "temperature", "multiple": True,
            }}},
            CONF_TEMPERATURE_OFFSET: {"required": False, "selector": {"number": {
                "min": -5, "max": 5, "step": 0.5, "unit_of_measurement": "°C", "mode": "box",
            }}},
        },
    )
)

LAST_SEEN_SELECTOR = selector.ObjectSelector(
    selector.ObjectSelectorConfig(
        multiple=True,
        label_field="temperature",
        description_field=CONF_LAST_SEEN,
        translation_key="last_seen_sensor",
        fields={
            "temperature": {"required": True, "selector": {"entity": {"domain": "sensor"}}},
            CONF_LAST_SEEN: {"required": True, "selector": {"entity": {"domain": "sensor"}}},
        },
    )
)


def _periods_for_form(periods: list[dict]) -> list[dict]:
    """Stored "HH:MM" periods as the time selector's "HH:MM:SS" values."""
    return [
        {CONF_START: f"{p[CONF_START]}:00", CONF_END: f"{p[CONF_END]}:00", CONF_TEMPERATURE: p[CONF_TEMPERATURE]}
        for p in periods
    ]


def _periods_from_form(items: list[dict] | None) -> list[dict] | None:
    """Validate and normalise a list of periods; None if a time is invalid."""
    periods = []
    for item in items or []:
        start, end = parse_time(item.get(CONF_START)), parse_time(item.get(CONF_END))
        try:
            temperature = float(item.get(CONF_TEMPERATURE))
        except (TypeError, ValueError):
            return None
        if start is None or end is None:
            return None
        periods.append({
            CONF_START: format_time(start % 1440),
            CONF_END: format_time(end % 1440),
            CONF_TEMPERATURE: temperature,
        })
    return sort_periods(periods)


def _subentries(entry: ConfigEntry, subentry_type: str) -> list[ConfigSubentry]:
    return [s for s in entry.subentries.values() if s.subentry_type == subentry_type]


# ---------------------------------------------------------------------------
# Add Integration (and YAML import)
# ---------------------------------------------------------------------------

class HeatingManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Integration wizard and YAML import."""

    VERSION = 3

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HeatingManagerOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ZONE: ZoneSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                zone_id = unique_id_for(name, set(), "zone_1")
                zone = {"name": name, CONF_SCHEDULE: {
                    CONF_WEEKDAY: [dict(DEFAULT_SCHEDULE_PERIOD)], CONF_WEEKEND: [dict(DEFAULT_SCHEDULE_PERIOD)],
                }}
                return self.async_create_entry(
                    title=TITLE,
                    data={},
                    options={OPT_SETTINGS: default_settings()},
                    subentries=[zone_subentry(zone_id, zone)],
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default="Home"): str}),
            errors=errors,
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry from heating_manager.yaml (converted by yaml_to_options)."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(
            title=TITLE,
            data={},
            options={OPT_SETTINGS: import_data.get(OPT_SETTINGS, {})},
            subentries=zones_to_subentries(import_data.get(OPT_ZONES, {})),
        )


# ---------------------------------------------------------------------------
# Zones (with their rooms)
# ---------------------------------------------------------------------------

def _room_values(room: dict) -> dict:
    """A stored room as an item of the form's Rooms list (the form can't carry ids)."""
    values: dict[str, Any] = {
        CONF_NAME: room.get(CONF_NAME, ""),
        CONF_TRVS: list(room.get(CONF_TRVS) or []),
        CONF_SENSORS: [s["temperature"] for s in room.get(CONF_SENSORS) or []],
    }
    if room.get(CONF_TEMPERATURE_OFFSET):
        values[CONF_TEMPERATURE_OFFSET] = room[CONF_TEMPERATURE_OFFSET]
    return values


def _zone_form_values(data: dict) -> dict:
    """Stored zone data as form values."""
    schedule = data.get(CONF_SCHEDULE) or {}
    same = data.get(CONF_WEEKEND_SAME, True)
    rooms = data.get(CONF_ROOMS) or []
    return {
        CONF_NAME: data.get(CONF_NAME, ""),
        CONF_ROOMS: [_room_values(room) for room in rooms],
        CONF_HEATING_DEMAND_MODE: data.get(CONF_HEATING_DEMAND_MODE, GLOBAL),
        CONF_MONITORING_ONLY: data.get(CONF_MONITORING_ONLY, False),
        CONF_WEEKDAY: {CONF_PERIODS: _periods_for_form(schedule.get(CONF_WEEKDAY, []))},
        CONF_WEEKEND: {
            CONF_SAME: same,
            CONF_PERIODS: [] if same else _periods_for_form(schedule.get(CONF_WEEKEND, [])),
        },
        CONF_ADVANCED: {CONF_LAST_SEEN: [
            {"temperature": sensor["temperature"], CONF_LAST_SEEN: sensor[CONF_LAST_SEEN]}
            for room in rooms
            for sensor in room.get(CONF_SENSORS) or []
            if sensor.get(CONF_LAST_SEEN)
        ]},
    }


class ZoneSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a zone: its rooms, schedules, demand mode and monitoring."""

    def _schema(self, values: dict) -> vol.Schema:
        weekday = values.get(CONF_WEEKDAY) or {}
        weekend = values.get(CONF_WEEKEND) or {}
        advanced = values.get(CONF_ADVANCED) or {}
        same = weekend.get(CONF_SAME, True)
        return vol.Schema({
            vol.Required(CONF_NAME, default=values.get(CONF_NAME, "")): str,
            vol.Optional(CONF_ROOMS, default=list(values.get(CONF_ROOMS) or [])): ROOMS_SELECTOR,
            vol.Required(
                CONF_HEATING_DEMAND_MODE, default=values.get(CONF_HEATING_DEMAND_MODE, GLOBAL)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[GLOBAL, *HEATING_DEMAND_MODES],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="zone_heating_demand_mode",
                )
            ),
            vol.Required(CONF_MONITORING_ONLY, default=values.get(CONF_MONITORING_ONLY, False)): bool,
            vol.Required(CONF_WEEKDAY): section(
                vol.Schema({
                    vol.Optional(CONF_PERIODS, default=list(weekday.get(CONF_PERIODS) or [])): PERIODS_SELECTOR,
                }),
                {"collapsed": False},
            ),
            vol.Required(CONF_WEEKEND): section(
                vol.Schema({
                    vol.Required(CONF_SAME, default=same): bool,
                    vol.Optional(CONF_PERIODS, default=list(weekend.get(CONF_PERIODS) or [])): PERIODS_SELECTOR,
                }),
                {"collapsed": same},
            ),
            vol.Required(CONF_ADVANCED): section(
                vol.Schema({
                    vol.Optional(CONF_LAST_SEEN, default=list(advanced.get(CONF_LAST_SEEN) or [])): LAST_SEEN_SELECTOR,
                }),
                {"collapsed": not advanced.get(CONF_LAST_SEEN)},
            ),
        })

    def _validate(
        self, user_input: dict, zone_id: str, old_rooms: list[dict]
    ) -> tuple[dict | None, dict[str, str], dict[str, str]]:
        """Return (zone data, errors, error placeholders)."""
        entry = self._get_entry()
        other_zones = [sub for sub in _subentries(entry, SUBENTRY_ZONE) if sub.data.get(CONF_ZONE_ID) != zone_id]
        errors: dict[str, str] = {}
        placeholders = {"overlap": "", "room": "", "trv_conflict": ""}

        name = user_input.get(CONF_NAME, "").strip()
        if not name:
            errors[CONF_NAME] = "name_required"
        elif any(sub.data.get(CONF_NAME, "").casefold() == name.casefold() for sub in other_zones):
            errors[CONF_NAME] = "name_in_use"

        rooms = [
            {**item, CONF_NAME: str(item.get(CONF_NAME, "")).strip()} for item in user_input.get(CONF_ROOMS) or []
        ]
        seen_names: set[str] = set()
        for room in rooms:
            if not room[CONF_NAME]:
                errors["base"], placeholders["room"] = "room_name_required", ""
            elif room[CONF_NAME].casefold() in seen_names:
                errors["base"], placeholders["room"] = "room_name_in_use", room[CONF_NAME]
            elif not room.get(CONF_TRVS) and not room.get(CONF_SENSORS):
                errors["base"], placeholders["room"] = "room_empty", room[CONF_NAME]
            seen_names.add(room[CONF_NAME].casefold())
            if "base" in errors:
                break

        if "base" not in errors:
            owners: dict[str, str] = {}
            for sub in other_zones:
                for other in sub.data.get(CONF_ROOMS) or []:
                    for trv in other.get(CONF_TRVS) or []:
                        owners.setdefault(trv, room_label_for(other.get(CONF_NAME, ""), sub.data.get(CONF_NAME, "")))
            conflicts = []
            for room in rooms:
                for trv in room.get(CONF_TRVS) or []:
                    if trv in owners:
                        conflicts.append(f"{trv} ({owners[trv]})")
                    owners[trv] = room_label_for(room[CONF_NAME], name)
            if conflicts:
                errors["base"] = "trv_in_use"
                placeholders["trv_conflict"] = ", ".join(conflicts)

        weekday_in = user_input.get(CONF_WEEKDAY) or {}
        weekend_in = user_input.get(CONF_WEEKEND) or {}
        same = bool(weekend_in.get(CONF_SAME, True))
        weekday = _periods_from_form(weekday_in.get(CONF_PERIODS))
        weekend = [] if same else _periods_from_form(weekend_in.get(CONF_PERIODS))
        if "base" not in errors:
            if weekday is None or weekend is None:
                errors["base"] = "invalid_time"
            else:
                for day, periods in (("Weekdays", weekday), ("Weekends", weekend)):
                    if overlap := first_overlap(periods):
                        errors["base"] = "overlap"
                        placeholders["overlap"] = f"{day}: {overlap}"
                        break
        if errors:
            return None, errors, placeholders

        last_seen = {
            item.get("temperature"): item.get(CONF_LAST_SEEN)
            for item in (user_input.get(CONF_ADVANCED) or {}).get(CONF_LAST_SEEN) or []
        }
        for room in rooms:
            room[CONF_SENSORS] = [
                {"temperature": sid, **({CONF_LAST_SEEN: last_seen[sid]} if last_seen.get(sid) else {})}
                for sid in room.get(CONF_SENSORS) or []
            ]
            room[CONF_TEMPERATURE_OFFSET] = float(room.get(CONF_TEMPERATURE_OFFSET) or 0.0)
        data: dict[str, Any] = {
            CONF_ZONE_ID: zone_id,
            CONF_NAME: name,
            CONF_SCHEDULE: {CONF_WEEKDAY: weekday, CONF_WEEKEND: weekday if same else weekend},
            CONF_WEEKEND_SAME: same,
            CONF_ROOMS: match_room_ids(rooms, old_rooms, all_room_ids(entry, exclude_zone=zone_id)),
        }
        if user_input.get(CONF_HEATING_DEMAND_MODE) in HEATING_DEMAND_MODES:
            data[CONF_HEATING_DEMAND_MODE] = user_input[CONF_HEATING_DEMAND_MODE]
        if user_input.get(CONF_MONITORING_ONLY):
            data[CONF_MONITORING_ONLY] = True
        return data, errors, placeholders

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        values = _zone_form_values({CONF_SCHEDULE: {CONF_WEEKDAY: [dict(DEFAULT_SCHEDULE_PERIOD)]}})
        errors: dict[str, str] = {}
        placeholders = {"overlap": "", "room": "", "trv_conflict": ""}
        if user_input is not None:
            taken = {sub.data.get(CONF_ZONE_ID) for sub in _subentries(self._get_entry(), SUBENTRY_ZONE)}
            zone_id = unique_id_for(user_input.get(CONF_NAME, "").strip(), taken, "zone")
            data, errors, placeholders = self._validate(user_input, zone_id, [])
            if data is not None:
                return self.async_create_entry(title=zone_title(data), data=data, unique_id=zone_id)
            values = user_input
        return self.async_show_form(
            step_id="user", data_schema=self._schema(values), errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        zone_id = subentry.data[CONF_ZONE_ID]
        values = _zone_form_values(dict(subentry.data))
        errors: dict[str, str] = {}
        placeholders = {"overlap": "", "room": "", "trv_conflict": ""}
        if user_input is not None:
            data, errors, placeholders = self._validate(
                user_input, zone_id, list(subentry.data.get(CONF_ROOMS) or [])
            )
            if data is not None:
                return self.async_update_and_abort(entry, subentry, title=zone_title(data), data=data)
            values = user_input
        return self.async_show_form(
            step_id="reconfigure", data_schema=self._schema(values), errors=errors,
            description_placeholders=placeholders,
        )


# ---------------------------------------------------------------------------
# Configure: settings and import
# ---------------------------------------------------------------------------

class HeatingManagerOptionsFlow(OptionsFlow):
    """Settings (one form in sections) and Import from YAML file."""

    def __init__(self) -> None:
        self._pending_import: dict[str, Any] | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "import_yaml"])

    def _current_settings(self) -> dict[str, Any]:
        settings = default_settings()
        settings.update(self.config_entry.options.get(OPT_SETTINGS) or {})
        return settings

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self._current_settings()
        if user_input is not None:
            for section_key, keys, _ in SETTINGS_SECTIONS:
                for key in keys:
                    if key in (user_input.get(section_key) or {}):
                        current[key] = coerce_setting(SETTINGS_BY_KEY[key], user_input[section_key][key])
            return self.async_create_entry(data={**self.config_entry.options, OPT_SETTINGS: current})
        schema = {
            vol.Required(section_key): section(
                vol.Schema({
                    vol.Required(key, default=current[key]): _setting_selector(SETTINGS_BY_KEY[key])
                    for key in keys
                }),
                {"collapsed": collapsed},
            )
            for section_key, keys, collapsed in SETTINGS_SECTIONS
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))

    # -- import from YAML ------------------------------------------------------

    def _load_yaml(self, path: str) -> tuple[dict | None, str | None, str]:
        """Read a heating_manager.yaml style file. Returns (content, error, full path)."""
        config_dir = os.path.realpath(self.hass.config.config_dir)
        full_path = os.path.realpath(
            path if os.path.isabs(path) else os.path.join(config_dir, path)
        )
        # Any file name is fine (e.g. a dated backup like heating_manager.yaml.20261008):
        # it must be inside the config folder and parse as a heating config.
        if os.path.commonpath([config_dir, full_path]) != config_dir:
            return None, "path_outside_config", full_path
        try:
            with open(full_path, encoding="utf-8") as file:
                content = yaml.safe_load(file)
        except FileNotFoundError:
            return None, "file_not_found", full_path
        except (OSError, yaml.YAMLError):
            return None, "invalid_yaml", full_path
        if not isinstance(content, dict):
            return None, "invalid_yaml", full_path
        return content, None, full_path

    async def async_step_import_yaml(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read zones (and optionally settings) from a YAML file."""
        errors: dict[str, str] = {}
        placeholders = {"keys": "", "path": ""}
        defaults = {CONF_PATH: DEFAULT_IMPORT_PATH, CONF_IMPORT_SETTINGS: True}
        if user_input is not None:
            defaults = user_input
            content, error, full_path = await self.hass.async_add_executor_job(
                self._load_yaml, user_input[CONF_PATH].strip()
            )
            placeholders["path"] = full_path
            if error:
                errors["base"] = error
            else:
                imported = yaml_to_options(content)
                if not imported[OPT_ZONES]:
                    errors["base"] = "no_zones"
                    placeholders["keys"] = ", ".join(map(str, content)) or "(none)"
                else:
                    settings = {
                        key: value
                        for key, value in imported[OPT_SETTINGS].items()
                        if key in content and key in SETTINGS_BY_KEY
                    } if user_input.get(CONF_IMPORT_SETTINGS) else {}
                    self._pending_import = {
                        OPT_ZONES: imported[OPT_ZONES],
                        OPT_SETTINGS: settings,
                        CONF_PATH: full_path,
                    }
                    return await self.async_step_import_yaml_confirm()
        return self.async_show_form(
            step_id="import_yaml",
            data_schema=vol.Schema({
                vol.Required(CONF_PATH, default=defaults[CONF_PATH]): str,
                vol.Optional(CONF_IMPORT_SETTINGS, default=defaults.get(CONF_IMPORT_SETTINGS, True)): bool,
            }),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_import_yaml_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        pending = self._pending_import
        existing_zones = entry_to_runtime(entry)[0]["zones"]

        if user_input is not None:
            self._pending_import = None
            if not user_input.get(CONF_CONFIRM):
                return await self.async_step_init()
            self._apply_import(pending[OPT_ZONES])
            settings = {**self._current_settings(), **pending[OPT_SETTINGS]}
            return self.async_create_entry(data={**entry.options, OPT_SETTINGS: settings})

        zone_lines = []
        for zone_id, zone in pending[OPT_ZONES].items():
            rooms = len(zone.get(CONF_ROOMS, {}))
            periods = sum(len(p) for p in zone.get(CONF_SCHEDULE, {}).values())
            replaces = " (replaces the existing zone)" if zone_id in existing_zones else ""
            zone_lines.append(
                f"- {zone.get(CONF_NAME, zone_id)}: {rooms} room(s), "
                f"{periods} schedule period(s){replaces}"
            )
        merged = copy.deepcopy(existing_zones)
        merged.update(copy.deepcopy(pending[OPT_ZONES]))
        duplicates = dedupe_trvs(merged)
        warning_blocks = []
        if duplicates:
            warning_blocks.append(
                "TRVs in more than one room (each will only control the first):\n"
                + "\n".join(f"- {d}" for d in duplicates)
            )
        overlaps = [
            problem
            for zone_id, zone in pending[OPT_ZONES].items()
            for problem in schedule_overlaps(zone.get(CONF_NAME, zone_id), zone.get(CONF_SCHEDULE, {}))
        ]
        if overlaps:
            warning_blocks.append(
                "Schedule periods that overlap (fix them in the zone after importing):\n"
                + "\n".join(f"- {o}" for o in overlaps)
            )
        warnings = "\n\n".join(warning_blocks)
        return self.async_show_form(
            step_id="import_yaml_confirm",
            data_schema=vol.Schema({vol.Required(CONF_CONFIRM, default=True): bool}),
            description_placeholders={
                "path": pending[CONF_PATH],
                "zones": "\n".join(zone_lines),
                "settings": str(len(pending[OPT_SETTINGS])),
                "warnings": warnings,
            },
        )

    def _apply_import(self, zones: dict[str, dict]) -> None:
        """Add the imported zones; a zone with the same id is replaced, keeping matching rooms' entities."""
        entry = self.config_entry
        config_entries = self.hass.config_entries
        existing = {sub.data.get(CONF_ZONE_ID): sub for sub in _subentries(entry, SUBENTRY_ZONE)}
        new_zones = {}
        for zone_id, zone in zones.items():
            sub = existing.get(zone_id)
            if sub is None:
                new_zones[zone_id] = zone
                continue
            rooms = [{**room, CONF_ROOM_ID: room_id} for room_id, room in (zone.get(CONF_ROOMS) or {}).items()]
            data = zone_subentry(zone_id, zone)["data"]
            data[CONF_ROOMS] = match_room_ids(
                rooms, list(sub.data.get(CONF_ROOMS) or []), all_room_ids(entry, exclude_zone=zone_id)
            )
            config_entries.async_update_subentry(entry, sub, data=data, title=zone_title(data))
        for data in zones_to_subentries(new_zones, taken_room_ids=all_room_ids(entry)):
            config_entries.async_add_subentry(entry, ConfigSubentry(**data))
