"""The Heating Manager integration."""
import logging
import os
from datetime import timedelta

import yaml
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
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
    CONF_MINIMUM_TEMP,
    CONF_TRV_COOLDOWN_OFFSET,
    CONF_TRV_OFFSET_EMA_ALPHA,
    CONF_TRV_OVERSHOOT_ENABLED,
    CONF_TRV_OVERSHOOT_MAX,
    CONF_TRV_OVERSHOOT_THRESHOLD,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ANALYTICS_ENABLED,
    DEFAULT_ANALYTICS_HISTORY_SIZE,
    DEFAULT_ANALYTICS_MIN_SAMPLES,
    DEFAULT_BOOST_DURATION,
    DEFAULT_DERIVATIVE_SMOOTHING,
    DEFAULT_FALLBACK_MODE,
    DEFAULT_FROST_PROTECTION_TEMP,
    DEFAULT_HEATING_DEADBAND,
    DEFAULT_MINIMUM_TEMP,
    DEFAULT_TRV_COOLDOWN_OFFSET,
    DEFAULT_TRV_OFFSET_EMA_ALPHA,
    DEFAULT_TRV_OVERSHOOT_ENABLED,
    DEFAULT_TRV_OVERSHOOT_MAX,
    DEFAULT_TRV_OVERSHOOT_THRESHOLD,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    SERVICE_SET_MODE,
)
from .coordinator import HeatingManagerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_CONFIG_FILE): cv.string,
                vol.Optional(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
                ): cv.positive_int,
                vol.Optional(
                    CONF_MINIMUM_TEMP, default=DEFAULT_MINIMUM_TEMP
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_FROST_PROTECTION_TEMP, default=DEFAULT_FROST_PROTECTION_TEMP
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_FALLBACK_MODE, default=DEFAULT_FALLBACK_MODE
                ): cv.string,
                vol.Optional(
                    CONF_HEATING_DEADBAND, default=DEFAULT_HEATING_DEADBAND
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_TRV_OVERSHOOT_ENABLED, default=DEFAULT_TRV_OVERSHOOT_ENABLED
                ): cv.boolean,
                vol.Optional(
                    CONF_TRV_OVERSHOOT_MAX, default=DEFAULT_TRV_OVERSHOOT_MAX
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_TRV_OVERSHOOT_THRESHOLD, default=DEFAULT_TRV_OVERSHOOT_THRESHOLD
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_TRV_COOLDOWN_OFFSET, default=DEFAULT_TRV_COOLDOWN_OFFSET
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_TRV_OFFSET_EMA_ALPHA, default=DEFAULT_TRV_OFFSET_EMA_ALPHA
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_ANALYTICS_ENABLED, default=DEFAULT_ANALYTICS_ENABLED
                ): cv.boolean,
                vol.Optional(
                    CONF_ANALYTICS_HISTORY_SIZE, default=DEFAULT_ANALYTICS_HISTORY_SIZE
                ): cv.positive_int,
                vol.Optional(
                    CONF_ANALYTICS_MIN_SAMPLES, default=DEFAULT_ANALYTICS_MIN_SAMPLES
                ): cv.positive_int,
                vol.Optional(
                    CONF_DERIVATIVE_SMOOTHING, default=DEFAULT_DERIVATIVE_SMOOTHING
                ): vol.Coerce(float),
            }
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
    """Set up the Heating Manager component."""
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]

    # Load the heating manager config file
    config_file = conf[CONF_CONFIG_FILE]
    if not os.path.isabs(config_file):
        config_file = hass.config.path(config_file)

    def load_config():
        """Load config file synchronously."""
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

    # Read values from heating_manager.yaml, with fallback to configuration.yaml, then defaults
    update_interval = heating_config.get(
        CONF_UPDATE_INTERVAL,
        conf.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    )
    minimum_temp = heating_config.get(
        CONF_MINIMUM_TEMP,
        conf.get(CONF_MINIMUM_TEMP, DEFAULT_MINIMUM_TEMP)
    )
    frost_protection_temp = heating_config.get(
        CONF_FROST_PROTECTION_TEMP,
        conf.get(CONF_FROST_PROTECTION_TEMP, DEFAULT_FROST_PROTECTION_TEMP)
    )
    fallback_mode = heating_config.get(
        CONF_FALLBACK_MODE,
        conf.get(CONF_FALLBACK_MODE, DEFAULT_FALLBACK_MODE)
    )
    boost_duration = heating_config.get(
        CONF_BOOST_DURATION,
        conf.get(CONF_BOOST_DURATION, DEFAULT_BOOST_DURATION)
    )
    heating_deadband = heating_config.get(
        CONF_HEATING_DEADBAND,
        conf.get(CONF_HEATING_DEADBAND, DEFAULT_HEATING_DEADBAND)
    )
    trv_overshoot_enabled = heating_config.get(
        CONF_TRV_OVERSHOOT_ENABLED,
        conf.get(CONF_TRV_OVERSHOOT_ENABLED, DEFAULT_TRV_OVERSHOOT_ENABLED)
    )
    trv_overshoot_max = heating_config.get(
        CONF_TRV_OVERSHOOT_MAX,
        conf.get(CONF_TRV_OVERSHOOT_MAX, DEFAULT_TRV_OVERSHOOT_MAX)
    )
    trv_overshoot_threshold = heating_config.get(
        CONF_TRV_OVERSHOOT_THRESHOLD,
        conf.get(CONF_TRV_OVERSHOOT_THRESHOLD, DEFAULT_TRV_OVERSHOOT_THRESHOLD)
    )
    trv_cooldown_offset = heating_config.get(
        CONF_TRV_COOLDOWN_OFFSET,
        conf.get(CONF_TRV_COOLDOWN_OFFSET, DEFAULT_TRV_COOLDOWN_OFFSET)
    )
    trv_offset_ema_alpha = heating_config.get(
        CONF_TRV_OFFSET_EMA_ALPHA,
        conf.get(CONF_TRV_OFFSET_EMA_ALPHA, DEFAULT_TRV_OFFSET_EMA_ALPHA)
    )
    analytics_enabled = heating_config.get(
        CONF_ANALYTICS_ENABLED,
        conf.get(CONF_ANALYTICS_ENABLED, DEFAULT_ANALYTICS_ENABLED)
    )
    analytics_history_size = heating_config.get(
        CONF_ANALYTICS_HISTORY_SIZE,
        conf.get(CONF_ANALYTICS_HISTORY_SIZE, DEFAULT_ANALYTICS_HISTORY_SIZE)
    )
    analytics_min_samples = heating_config.get(
        CONF_ANALYTICS_MIN_SAMPLES,
        conf.get(CONF_ANALYTICS_MIN_SAMPLES, DEFAULT_ANALYTICS_MIN_SAMPLES)
    )
    derivative_smoothing = heating_config.get(
        CONF_DERIVATIVE_SMOOTHING,
        conf.get(CONF_DERIVATIVE_SMOOTHING, DEFAULT_DERIVATIVE_SMOOTHING)
    )

    # Create coordinator
    coordinator = HeatingManagerCoordinator(
        hass,
        heating_config,
        update_interval,
        minimum_temp,
        frost_protection_temp,
        fallback_mode,
        boost_duration,
        heating_deadband,
        trv_overshoot_enabled,
        trv_overshoot_max,
        trv_overshoot_threshold,
        trv_cooldown_offset,
        trv_offset_ema_alpha,
        analytics_enabled,
        analytics_history_size,
        analytics_min_samples,
        derivative_smoothing,
    )

    # Ensure first update completes before entities are created
    try:
        await coordinator.async_refresh()
        if not coordinator.data:
            _LOGGER.warning(
                "Coordinator first refresh completed but no data available. "
                "Entities may not have target temperatures until next update."
            )
    except Exception as err:
        _LOGGER.error(
            "Failed to perform initial coordinator refresh: %s. "
            "Heating manager may not work correctly until next update.",
            err,
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["coordinator"] = coordinator

    # Load platforms using discovery
    for platform in PLATFORMS:
        hass.async_create_task(
            async_load_platform(hass, platform, DOMAIN, {}, config)
        )

    # Register global services
    async def handle_set_mode(call: ServiceCall) -> None:
        """Handle the set_mode service call."""
        mode = call.data[ATTR_MODE]
        if mode == "away":
            await coordinator.set_away_mode(True)
        elif mode == "schedule":
            await coordinator.set_away_mode(False)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_MODE, handle_set_mode, schema=SERVICE_SET_MODE_SCHEMA
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
