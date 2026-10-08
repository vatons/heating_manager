"""Config and options flows for Heating Manager (UI setup)."""
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
    OptionsFlow,
)
from homeassistant.core import callback
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
    HEATING_DEMAND_MODES,
    OPT_SETTINGS,
    OPT_ZONES,
    SETTINGS,
    SETTINGS_BY_KEY,
    SettingSpec,
    coerce_setting,
    format_period,
    new_options,
    new_zone,
    period_minutes,
    sort_periods,
    unique_id_for,
    yaml_to_options,
)
from .schedule_manager import format_time, parse_time

TITLE = "Heating Manager"

ADD = "__add__"
SAVE = "__save__"
BACK = "__back__"
COPY = "__copy__"
GLOBAL = "global"

CONF_NAME = "name"
CONF_ZONE = "zone"
CONF_ROOM = "room"
CONF_DAY = "day"
CONF_PERIOD = "period"
CONF_DELETE = "delete"
CONF_CONFIRM = "confirm"
CONF_LAST_SEEN = "configure_last_seen"
CONF_PATH = "path"
CONF_IMPORT_SETTINGS = "import_settings"
DEFAULT_IMPORT_PATH = "heating_manager.yaml"


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


def _choice(options: list[dict[str, str]]) -> selector.SelectSelector:
    """A list of choices, always ending with 'Save and close'."""
    options = [*options, {"value": SAVE, "label": "💾 Save and close"}]
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.LIST)
    )


TEMPERATURE_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=5, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX
    )
)


class HeatingManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Integration wizard and YAML import."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HeatingManagerOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                return self.async_create_entry(title=TITLE, data={}, options=new_options(name))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default="Home"): str}),
            errors=errors,
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry from heating_manager.yaml (converted by yaml_to_options)."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(title=TITLE, data={}, options=import_data)


class HeatingManagerOptionsFlow(OptionsFlow):
    """Configure menu: zones, rooms, schedules and settings.

    Edits are collected in memory and written when "Save and close" is chosen.
    """

    def __init__(self) -> None:
        self._options: dict[str, Any] | None = None
        self._zone_id: str | None = None
        self._room_id: str | None = None
        self._day: str | None = None
        self._period_index: int | None = None
        self._pending_import: dict[str, Any] | None = None

    @property
    def options(self) -> dict[str, Any]:
        if self._options is None:
            self._options = copy.deepcopy(dict(self.config_entry.options))
            self._options.setdefault(OPT_SETTINGS, {})
            self._options.setdefault(OPT_ZONES, {})
        return self._options

    @property
    def _zones(self) -> dict[str, dict]:
        return self.options[OPT_ZONES]

    @property
    def _zone(self) -> dict:
        return self._zones[self._zone_id]

    # -- main menu ---------------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["zones", "settings", "advanced", "import_yaml", "save"]
        )

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(data=self.options)

    # -- import from YAML ----------------------------------------------------

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
        pending = self._pending_import
        if user_input is not None:
            if user_input.get(CONF_CONFIRM):
                self._zones.update(pending[OPT_ZONES])
                self.options[OPT_SETTINGS].update(pending[OPT_SETTINGS])
            self._pending_import = None
            return await self.async_step_init()

        zone_lines = []
        for zone_id, zone in pending[OPT_ZONES].items():
            rooms = len(zone.get(CONF_ROOMS, {}))
            periods = sum(len(p) for p in zone.get(CONF_SCHEDULE, {}).values())
            replaces = " (replaces the existing zone)" if zone_id in self._zones else ""
            zone_lines.append(
                f"- {zone.get(CONF_NAME, zone_id)}: {rooms} room(s), "
                f"{periods} schedule period(s){replaces}"
            )
        return self.async_show_form(
            step_id="import_yaml_confirm",
            data_schema=vol.Schema({vol.Required(CONF_CONFIRM, default=True): bool}),
            description_placeholders={
                "path": pending[CONF_PATH],
                "zones": "\n".join(zone_lines),
                "settings": str(len(pending[OPT_SETTINGS])),
            },
        )

    # -- settings ----------------------------------------------------------

    async def _settings_step(
        self, step_id: str, advanced: bool, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        specs = [spec for spec in SETTINGS if spec.advanced == advanced]
        current = self.options[OPT_SETTINGS]
        if user_input is not None:
            for spec in specs:
                if spec.key in user_input:
                    current[spec.key] = coerce_setting(spec, user_input[spec.key])
            return await self.async_step_init()
        schema = vol.Schema({
            vol.Required(spec.key, default=current.get(spec.key, spec.default)): _setting_selector(spec)
            for spec in specs
        })
        return self.async_show_form(step_id=step_id, data_schema=schema)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._settings_step("settings", False, user_input)

    async def async_step_advanced(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._settings_step("advanced", True, user_input)

    # -- zones -------------------------------------------------------------

    async def async_step_zones(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            choice = user_input[CONF_ZONE]
            if choice == SAVE:
                return await self.async_step_save()
            if choice == BACK:
                return await self.async_step_init()
            if choice == ADD:
                self._zone_id = None
                return await self.async_step_zone_edit()
            self._zone_id = choice
            return await self.async_step_zone()
        choices = [{"value": zid, "label": z.get(CONF_NAME, zid)} for zid, z in self._zones.items()]
        choices += [
            {"value": ADD, "label": "➕ Add a zone"},
            {"value": BACK, "label": "↩ Back"},
        ]
        return self.async_show_form(
            step_id="zones", data_schema=vol.Schema({vol.Required(CONF_ZONE): _choice(choices)})
        )

    async def async_step_zone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="zone",
            menu_options=["rooms", "schedule", "zone_edit", "zone_delete", "zones", "save"],
            description_placeholders={"zone": self._zone.get(CONF_NAME, self._zone_id)},
        )

    async def async_step_zone_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        zone = self._zone if self._zone_id else None
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            else:
                if zone is None:
                    self._zone_id = unique_id_for(name, self._zones, "zone")
                    zone = self._zones[self._zone_id] = new_zone(name)
                zone[CONF_NAME] = name
                mode = user_input[CONF_HEATING_DEMAND_MODE]
                if mode == GLOBAL:
                    zone.pop(CONF_HEATING_DEMAND_MODE, None)
                else:
                    zone[CONF_HEATING_DEMAND_MODE] = mode
                if user_input.get(CONF_MONITORING_ONLY):
                    zone[CONF_MONITORING_ONLY] = True
                else:
                    zone.pop(CONF_MONITORING_ONLY, None)
                return await self.async_step_zone()
        schema = vol.Schema({
            vol.Required(CONF_NAME, default=zone.get(CONF_NAME, "") if zone else ""): str,
            vol.Required(
                CONF_HEATING_DEMAND_MODE,
                default=(zone or {}).get(CONF_HEATING_DEMAND_MODE, GLOBAL),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[GLOBAL, *HEATING_DEMAND_MODES],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="zone_heating_demand_mode",
                )
            ),
            vol.Optional(
                CONF_MONITORING_ONLY, default=(zone or {}).get(CONF_MONITORING_ONLY, False)
            ): bool,
        })
        return self.async_show_form(step_id="zone_edit", data_schema=schema, errors=errors)

    async def async_step_zone_delete(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get(CONF_CONFIRM):
                del self._zones[self._zone_id]
                self._zone_id = None
                return await self.async_step_zones()
            return await self.async_step_zone()
        return self.async_show_form(
            step_id="zone_delete",
            data_schema=vol.Schema({vol.Required(CONF_CONFIRM, default=False): bool}),
            description_placeholders={"zone": self._zone.get(CONF_NAME, self._zone_id)},
        )

    # -- rooms -------------------------------------------------------------

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        rooms = self._zone[CONF_ROOMS]
        if user_input is not None:
            choice = user_input[CONF_ROOM]
            if choice == SAVE:
                return await self.async_step_save()
            if choice == BACK:
                return await self.async_step_zone()
            self._room_id = None if choice == ADD else choice
            return await self.async_step_room()
        choices = [{"value": rid, "label": r.get(CONF_NAME, rid)} for rid, r in rooms.items()]
        choices += [
            {"value": ADD, "label": "➕ Add a room"},
            {"value": BACK, "label": "↩ Back"},
        ]
        return self.async_show_form(
            step_id="rooms",
            data_schema=vol.Schema({vol.Required(CONF_ROOM): _choice(choices)}),
            description_placeholders={"zone": self._zone.get(CONF_NAME, self._zone_id)},
        )

    async def async_step_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        rooms = self._zone[CONF_ROOMS]
        room = rooms.get(self._room_id) if self._room_id else None
        errors: dict[str, str] = {}
        if user_input is not None:
            if room is not None and user_input.get(CONF_DELETE):
                del rooms[self._room_id]
                self._room_id = None
                return await self.async_step_rooms()
            name = user_input[CONF_NAME].strip()
            trvs = list(user_input.get(CONF_TRVS) or [])
            sensor_ids = list(user_input.get(CONF_SENSORS) or [])
            if not name:
                errors[CONF_NAME] = "name_required"
            elif not trvs and not sensor_ids:
                errors["base"] = "room_empty"
            else:
                if room is None:
                    # "zone" is reserved: the zone entity's unique id ends in "_zone"
                    self._room_id = unique_id_for(name, {*rooms, "zone"}, "room")
                    room = rooms[self._room_id] = {}
                old_last_seen = {
                    s["temperature"]: s.get("last_seen") for s in room.get(CONF_SENSORS, [])
                }
                room[CONF_NAME] = name
                room[CONF_TRVS] = trvs
                room[CONF_SENSORS] = [
                    {"temperature": sid, **({"last_seen": old_last_seen[sid]} if old_last_seen.get(sid) else {})}
                    for sid in sensor_ids
                ]
                offset = float(user_input.get(CONF_TEMPERATURE_OFFSET) or 0.0)
                if offset:
                    room[CONF_TEMPERATURE_OFFSET] = offset
                else:
                    room.pop(CONF_TEMPERATURE_OFFSET, None)
                if user_input.get(CONF_LAST_SEEN) and sensor_ids:
                    return await self.async_step_room_last_seen()
                return await self.async_step_rooms()

        room = room or {}
        fields: dict[Any, Any] = {
            vol.Required(CONF_NAME, default=room.get(CONF_NAME, "")): str,
            vol.Optional(CONF_TRVS, default=room.get(CONF_TRVS, [])): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Optional(
                CONF_SENSORS, default=[s["temperature"] for s in room.get(CONF_SENSORS, [])]
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="temperature", multiple=True
                )
            ),
            vol.Optional(
                CONF_TEMPERATURE_OFFSET, default=room.get(CONF_TEMPERATURE_OFFSET, 0.0)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-5, max=5, step=0.5, unit_of_measurement="°C",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(CONF_LAST_SEEN, default=False): bool,
        }
        if self._room_id:
            fields[vol.Optional(CONF_DELETE, default=False)] = bool
        return self.async_show_form(
            step_id="room",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"zone": self._zone.get(CONF_NAME, self._zone_id)},
        )

    async def async_step_room_last_seen(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally map each temperature sensor to a 'last seen' timestamp sensor."""
        room = self._zone[CONF_ROOMS][self._room_id]
        if user_input is not None:
            for sensor in room[CONF_SENSORS]:
                last_seen = user_input.get(sensor["temperature"])
                if last_seen:
                    sensor["last_seen"] = last_seen
                else:
                    sensor.pop("last_seen", None)
            return await self.async_step_rooms()
        fields = {}
        for sensor in room[CONF_SENSORS]:
            key = vol.Optional(
                sensor["temperature"],
                description={"suggested_value": sensor.get("last_seen")},
            )
            fields[key] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(
            step_id="room_last_seen",
            data_schema=vol.Schema(fields),
            description_placeholders={"room": room.get(CONF_NAME, self._room_id)},
        )

    # -- schedule ----------------------------------------------------------

    def _periods(self) -> list[dict]:
        schedule = self._zone.setdefault(CONF_SCHEDULE, {})
        return schedule.setdefault(self._day, [])

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_DAY] == BACK:
                return await self.async_step_zone()
            self._day = user_input[CONF_DAY]
            return await self.async_step_schedule_day()
        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema({
                vol.Required(CONF_DAY, default=CONF_WEEKDAY): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[CONF_WEEKDAY, CONF_WEEKEND, BACK],
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key="schedule_day",
                    )
                )
            }),
            description_placeholders={"zone": self._zone.get(CONF_NAME, self._zone_id)},
        )

    async def async_step_schedule_day(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        periods = self._periods()
        other = CONF_WEEKEND if self._day == CONF_WEEKDAY else CONF_WEEKDAY
        if user_input is not None:
            choice = user_input[CONF_PERIOD]
            if choice == SAVE:
                return await self.async_step_save()
            if choice == BACK:
                return await self.async_step_schedule()
            if choice == COPY:
                source = self._zone.get(CONF_SCHEDULE, {}).get(other, [])
                periods[:] = copy.deepcopy(source)
                return await self.async_step_schedule_day()
            self._period_index = None if choice == ADD else int(choice)
            return await self.async_step_period()
        choices = [{"value": str(i), "label": format_period(p)} for i, p in enumerate(periods)]
        choices += [
            {"value": ADD, "label": "➕ Add a period"},
            {"value": COPY, "label": f"⧉ Replace with the {other} schedule"},
            {"value": BACK, "label": "↩ Back"},
        ]
        return self.async_show_form(
            step_id="schedule_day",
            data_schema=vol.Schema({vol.Required(CONF_PERIOD): _choice(choices)}),
            description_placeholders={
                "zone": self._zone.get(CONF_NAME, self._zone_id),
                "day": self._day,
            },
        )

    async def async_step_period(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        periods = self._periods()
        existing = periods[self._period_index] if self._period_index is not None else None
        errors: dict[str, str] = {}
        if user_input is not None:
            if existing is not None and user_input.get(CONF_DELETE):
                periods.pop(self._period_index)
                return await self.async_step_schedule_day()
            start, end = parse_time(user_input[CONF_START]), parse_time(user_input[CONF_END])
            if start is None or end is None:
                errors["base"] = "invalid_time"
            else:
                period = {
                    CONF_START: format_time(start % 1440),
                    CONF_END: format_time(end % 1440),
                    CONF_TEMPERATURE: float(user_input[CONF_TEMPERATURE]),
                }
                others = [p for i, p in enumerate(periods) if i != self._period_index]
                if any(period_minutes(period) & period_minutes(p) for p in others):
                    errors["base"] = "overlap"
                else:
                    periods[:] = sort_periods([*others, period])
                    return await self.async_step_schedule_day()

        defaults = existing or {CONF_START: "06:30", CONF_END: "22:00", CONF_TEMPERATURE: 19.0}
        if user_input is not None:
            defaults = {**defaults, **{k: user_input[k] for k in (CONF_START, CONF_END, CONF_TEMPERATURE) if k in user_input}}
        fields: dict[Any, Any] = {
            vol.Required(CONF_START, default=defaults[CONF_START]): selector.TimeSelector(),
            vol.Required(CONF_END, default=defaults[CONF_END]): selector.TimeSelector(),
            vol.Required(CONF_TEMPERATURE, default=defaults[CONF_TEMPERATURE]): TEMPERATURE_SELECTOR,
        }
        if existing is not None:
            fields[vol.Optional(CONF_DELETE, default=False)] = bool
        return self.async_show_form(
            step_id="period", data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"day": self._day},
        )
