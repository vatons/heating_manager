"""The Heating Manager integration.

Set up from the UI (config entry). A legacy `heating_manager:` block in
configuration.yaml is imported into a config entry once, after which the UI is
the source of truth and a repair notice asks for the YAML to be removed.
"""
from __future__ import annotations

import inspect
import logging
import os

import voluptuous as vol
import yaml

from homeassistant.config_entries import (
    SOURCE_IMPORT,
    ConfigEntry,
    ConfigEntryState,
    ConfigSubentry,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_MODE,
    CONF_ANALYTICS_ENABLED,
    CONF_ANALYTICS_HISTORY_SIZE,
    CONF_ANALYTICS_MIN_SAMPLES,
    CONF_BOOST_DURATION,
    CONF_CONFIG_FILE,
    CONF_DERIVATIVE_SMOOTHING,
    CONF_FALLBACK_MODE,
    CONF_FROST_PROTECTION_TEMP,
    CONF_HEATING_DEADBAND,
    CONF_MIN_BOILER_OFF_TIME,
    CONF_MIN_BOILER_ON_TIME,
    CONF_MINIMUM_TEMP,
    CONF_TRV_COOLDOWN_OFFSET,
    CONF_TRV_OFFSET_EMA_ALPHA,
    CONF_TRV_OVERSHOOT_ENABLED,
    CONF_TRV_OVERSHOOT_MAX,
    CONF_TRV_OVERSHOOT_THRESHOLD,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    SERVICE_SET_MODE,
)
from .climate import async_link_room_devices
from .coordinator import HeatingManagerCoordinator
from .entry_data import (
    CONF_ROOM_ID,
    CONF_ZONE_ID,
    SUBENTRY_ROOM,
    SUBENTRY_ZONE,
    entry_to_runtime,
    legacy_unique_id_map,
    stored_room,
    yaml_to_options,
    zone_title,
    zones_to_subentries,
)

_LOGGER = logging.getLogger(__name__)

# Sensor, button and number: each room's boost entities, on the room's device
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.BUTTON, Platform.NUMBER]

type HeatingManagerConfigEntry = ConfigEntry[HeatingManagerCoordinator]

# Legacy YAML: only used to import into a config entry. Settings are optional
# here; anything not given falls back to heating_manager.yaml, then defaults.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_CONFIG_FILE): cv.string,
                vol.Optional(CONF_UPDATE_INTERVAL): cv.positive_int,
                vol.Optional(CONF_MINIMUM_TEMP): vol.Coerce(float),
                vol.Optional(CONF_FROST_PROTECTION_TEMP): vol.Coerce(float),
                vol.Optional(CONF_FALLBACK_MODE): cv.string,
                vol.Optional(CONF_BOOST_DURATION): cv.positive_int,
                vol.Optional(CONF_HEATING_DEADBAND): vol.Coerce(float),
                vol.Optional(CONF_TRV_OVERSHOOT_ENABLED): cv.boolean,
                vol.Optional(CONF_TRV_OVERSHOOT_MAX): vol.Coerce(float),
                vol.Optional(CONF_TRV_OVERSHOOT_THRESHOLD): vol.Coerce(float),
                vol.Optional(CONF_TRV_COOLDOWN_OFFSET): vol.Coerce(float),
                vol.Optional(CONF_TRV_OFFSET_EMA_ALPHA): vol.Coerce(float),
                vol.Optional(CONF_ANALYTICS_ENABLED): cv.boolean,
                vol.Optional(CONF_ANALYTICS_HISTORY_SIZE): cv.positive_int,
                vol.Optional(CONF_ANALYTICS_MIN_SAMPLES): cv.positive_int,
                vol.Optional(CONF_DERIVATIVE_SMOOTHING): vol.Coerce(float),
            },
            extra=vol.ALLOW_EXTRA,
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SERVICE_SET_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MODE): vol.In(["schedule", "away"]),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services and import legacy YAML configuration."""

    async def handle_set_mode(call: ServiceCall) -> None:
        """Handle the set_mode service call."""
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not entries:
            raise ServiceValidationError("Heating Manager is not set up")
        coordinator: HeatingManagerCoordinator = entries[0].runtime_data
        await coordinator.set_away_mode(call.data[ATTR_MODE] == "away")

    hass.services.async_register(
        DOMAIN, SERVICE_SET_MODE, handle_set_mode, schema=SERVICE_SET_MODE_SCHEMA
    )

    if DOMAIN in config:
        return await _async_import_yaml(hass, config[DOMAIN])
    return True


async def _async_import_yaml(hass: HomeAssistant, conf: dict) -> bool:
    config_file = conf[CONF_CONFIG_FILE]
    if not os.path.isabs(config_file):
        config_file = hass.config.path(config_file)

    def load_config():
        with open(config_file, "r") as f:
            return yaml.safe_load(f)

    try:
        heating_config = await hass.async_add_executor_job(load_config)
    except FileNotFoundError:
        _LOGGER.error("Heating manager config file not found: %s", config_file)
        return False
    except yaml.YAMLError as err:
        _LOGGER.error("Error parsing heating manager config: %s", err)
        return False

    if not isinstance(heating_config, dict):
        _LOGGER.error("Heating manager config file %s is empty or not a mapping", config_file)
        return False

    ir.async_create_issue(
        hass,
        DOMAIN,
        "yaml_imported",
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="yaml_imported",
        translation_placeholders={"config_file": config_file},
    )

    if hass.config_entries.async_entries(DOMAIN):
        # Already imported: the UI is the source of truth now
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=yaml_to_options(heating_config, conf),
        )
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: HeatingManagerConfigEntry) -> bool:
    """Migrate old config entries.

    Version 1 (2.x) kept zones and rooms in the entry's options. Version 2
    (3.0/3.1) made each zone and each room a subentry. Version 3 (3.2+) keeps
    each zone's rooms inside the zone's subentry.
    """
    if entry.version > 3:
        return False
    if entry.version == 1:
        zones = dict(entry.options.get("zones") or {})
        for data in zones_to_subentries(zones):
            hass.config_entries.async_add_subentry(entry, ConfigSubentry(**data))
        hass.config_entries.async_update_entry(
            entry, options={"settings": dict(entry.options.get("settings") or {})}, version=3
        )
        _LOGGER.info("Migrated Heating Manager: %d zone(s) and their rooms are now on the integration page", len(zones))
    if entry.version == 2:
        _async_move_rooms_into_zones(hass, entry)
        hass.config_entries.async_update_entry(entry, version=3)
    return True


@callback
def _async_move_rooms_into_zones(hass: HomeAssistant, entry: HeatingManagerConfigEntry) -> None:
    """3.0/3.1 -> 3.2: add each room subentry to its zone, keeping its entity and device."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    zones = {
        sub.data.get(CONF_ZONE_ID): sub for sub in entry.subentries.values() if sub.subentry_type == SUBENTRY_ZONE
    }
    rooms_by_zone: dict[str, list[dict]] = {zone_id: [] for zone_id in zones}
    for sub in list(entry.subentries.values()):
        if sub.subentry_type != SUBENTRY_ROOM:
            continue
        zone = zones.get(sub.data.get(CONF_ZONE_ID))
        if zone is not None:
            rooms_by_zone[sub.data[CONF_ZONE_ID]].append(stored_room(sub.data[CONF_ROOM_ID], dict(sub.data)))
            # Move the room's entities and devices first: removing the subentry removes what it still owns
            for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
                if entity.config_subentry_id == sub.subentry_id:
                    ent_reg.async_update_entity(entity.entity_id, config_subentry_id=zone.subentry_id)
            for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
                if sub.subentry_id in _device_subentries(device, entry.entry_id):
                    _move_device(dev_reg, device, entry.entry_id, sub.subentry_id, zone.subentry_id)
        hass.config_entries.async_remove_subentry(entry, sub.subentry_id)
    for zone_id, zone in zones.items():
        data = {**zone.data, "rooms": rooms_by_zone[zone_id]}
        hass.config_entries.async_update_subentry(entry, zone, data=data, title=zone_title(data))


def _device_subentries(device: dr.DeviceEntry, entry_id: str) -> set[str | None]:
    """The subentries of entry_id a device belongs to (one, from HA 2026.9)."""
    if hasattr(device, "config_subentry_id"):
        return {device.config_subentry_id} if device.config_entry_id == entry_id else set()
    return set(device.config_entries_subentries.get(entry_id, set()))


def _move_device(dev_reg: dr.DeviceRegistry, device: dr.DeviceEntry, entry_id: str, old: str, new: str) -> None:
    if "new_config_subentry_id" in inspect.signature(dev_reg.async_update_device).parameters:
        dev_reg.async_update_device(device.id, new_config_subentry_id=new)
    else:   # before devices belonged to a single subentry
        dev_reg.async_update_device(
            device.id,
            add_config_entry_id=entry_id,
            add_config_subentry_id=new,
            remove_config_entry_id=entry_id,
            remove_config_subentry_id=old,
        )


@callback
def _async_update_titles(hass: HomeAssistant, entry: HeatingManagerConfigEntry) -> None:
    """Keep zone titles (with their summaries) in step with the config."""
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type == SUBENTRY_ZONE and subentry.title != (title := zone_title(subentry.data)):
            hass.config_entries.async_update_subentry(entry, subentry, title=title)


def _runtime_signature(entry: HeatingManagerConfigEntry) -> str:
    """What the running setup depends on; a change that leaves this alone needs no reload."""
    return repr(entry_to_runtime(entry))


async def async_setup_entry(hass: HomeAssistant, entry: HeatingManagerConfigEntry) -> bool:
    """Set up Heating Manager from a config entry."""
    _async_update_titles(hass, entry)
    config, settings = entry_to_runtime(entry)
    _async_migrate_unique_ids(hass, config)

    coordinator = HeatingManagerCoordinator(
        hass,
        config,
        update_interval=settings[CONF_UPDATE_INTERVAL],
        minimum_temp=settings[CONF_MINIMUM_TEMP],
        frost_protection_temp=settings[CONF_FROST_PROTECTION_TEMP],
        fallback_mode=settings[CONF_FALLBACK_MODE],
        boost_duration=settings[CONF_BOOST_DURATION],
        heating_deadband=settings[CONF_HEATING_DEADBAND],
        trv_overshoot_enabled=settings[CONF_TRV_OVERSHOOT_ENABLED],
        trv_overshoot_max=settings[CONF_TRV_OVERSHOOT_MAX],
        trv_overshoot_threshold=settings[CONF_TRV_OVERSHOOT_THRESHOLD],
        trv_cooldown_offset=settings[CONF_TRV_COOLDOWN_OFFSET],
        trv_offset_ema_alpha=settings[CONF_TRV_OFFSET_EMA_ALPHA],
        analytics_enabled=settings[CONF_ANALYTICS_ENABLED],
        analytics_history_size=settings[CONF_ANALYTICS_HISTORY_SIZE],
        analytics_min_samples=settings[CONF_ANALYTICS_MIN_SAMPLES],
        derivative_smoothing=settings[CONF_DERIVATIVE_SMOOTHING],
        min_boiler_on_time=settings[CONF_MIN_BOILER_ON_TIME],
        min_boiler_off_time=settings[CONF_MIN_BOILER_OFF_TIME],
        config_entry=entry,
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.entry_signature = _runtime_signature(entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_link_room_devices(hass, entry, config.get("zones", {}))
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


@callback
def _async_migrate_unique_ids(hass: HomeAssistant, config: dict) -> None:
    """Move entities from older unique ids to the current ones, keeping their entity ids.

    Includes entities first created by the old YAML platform (no config entry).
    """
    ent_reg = er.async_get(hass)
    mapping = legacy_unique_id_map(config.get("zones", {}))
    for entity in list(ent_reg.entities.values()):
        if entity.platform != DOMAIN or entity.domain != Platform.CLIMATE:
            continue
        new_unique_id = mapping.get(entity.unique_id)
        if new_unique_id is None:
            continue
        if ent_reg.async_get_entity_id(Platform.CLIMATE, DOMAIN, new_unique_id):
            continue
        _LOGGER.debug("Migrating %s unique id %s -> %s", entity.entity_id, entity.unique_id, new_unique_id)
        ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)


async def _async_reload_entry(hass: HomeAssistant, entry: HeatingManagerConfigEntry) -> None:
    """Apply changes to settings, zones and rooms (title-only updates need no reload)."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None and getattr(coordinator, "entry_signature", None) == _runtime_signature(entry):
        _async_update_titles(hass, entry)
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HeatingManagerConfigEntry) -> bool:
    """Unload a config entry, saving learned state first."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        await coordinator.async_shutdown()
        await coordinator.async_save_state()
        ir.async_delete_issue(hass, DOMAIN, "missing_entities")
    return unloaded
