"""Constants for the Heating Manager integration."""
from datetime import timedelta

DOMAIN = "heating_manager"

# Configuration keys
CONF_CONFIG_FILE = "config_file"
CONF_ZONES = "zones"
CONF_ROOMS = "rooms"
CONF_SCHEDULE = "schedule"
CONF_TRVS = "trvs"
CONF_SENSORS = "sensors"
CONF_TEMPERATURE_OFFSET = "temperature_offset"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_FALLBACK_MODE = "fallback_mode"
CONF_MINIMUM_TEMP = "minimum_temp"
CONF_FROST_PROTECTION_TEMP = "frost_protection_temp"
CONF_AWAY_MODE = "away_mode"
CONF_HEATING_DEMAND_MODE = "heating_demand_mode"
CONF_BOOST_DURATION = "boost_duration"
CONF_HEATING_DEADBAND = "heating_deadband"
CONF_TRV_OVERSHOOT_ENABLED = "trv_overshoot_enabled"
CONF_TRV_OVERSHOOT_MAX = "trv_overshoot_max"
CONF_TRV_OVERSHOOT_THRESHOLD = "trv_overshoot_threshold"
CONF_TRV_COOLDOWN_OFFSET = "trv_cooldown_offset"
CONF_TRV_OFFSET_EMA_ALPHA = "trv_offset_ema_alpha"

# Heating Analytics Configuration
CONF_ANALYTICS_ENABLED = "analytics_enabled"
CONF_ANALYTICS_HISTORY_SIZE = "analytics_history_size"
CONF_ANALYTICS_MIN_SAMPLES = "analytics_min_samples"
CONF_DERIVATIVE_SMOOTHING = "derivative_smoothing_factor"
CONF_MAX_TEMP_CHANGE_PER_MIN = "max_temp_change_per_minute"

# Schedule keys
CONF_WEEKDAY = "weekday"
CONF_WEEKEND = "weekend"
CONF_START = "start"
CONF_END = "end"
CONF_TEMPERATURE = "temperature"

# Defaults
DEFAULT_UPDATE_INTERVAL = 60 # seconds
DEFAULT_MINIMUM_TEMP = 15.0 # degrees
DEFAULT_FROST_PROTECTION_TEMP = 15.0 # degrees
DEFAULT_BOOST_DURATION = 30  # minutes
DEFAULT_BOOST_TEMP_INCREASE = 2.0  # degrees
DEFAULT_SENSOR_TIMEOUT = 30  # minutes
DEFAULT_FALLBACK_MODE = "zone_average"
DEFAULT_HEATING_DEMAND_MODE = "any_room"
DEFAULT_HEATING_DEADBAND = 0.3  # degrees (°C)
DEFAULT_TRV_OVERSHOOT_ENABLED = True
DEFAULT_TRV_OVERSHOOT_MAX = 10.0  # degrees (°C)
DEFAULT_TRV_OVERSHOOT_THRESHOLD = 0.3  # degrees (°C)
DEFAULT_TRV_COOLDOWN_OFFSET = 1.0  # degrees (°C)
DEFAULT_TRV_OFFSET_EMA_ALPHA = 0.15  # EMA smoothing factor (0.1=stable, 0.2=responsive)

# Analytics defaults
DEFAULT_ANALYTICS_ENABLED = True
DEFAULT_ANALYTICS_HISTORY_SIZE = 30  # Keep last 30 readings per room
DEFAULT_ANALYTICS_MIN_SAMPLES = 3    # Need at least 3 samples for derivative
DEFAULT_DERIVATIVE_SMOOTHING = 0.3   # EMA smoothing factor for derivatives
DEFAULT_MAX_TEMP_CHANGE_PER_MIN = 0.5  # Max plausible °C/min change

# Heating logic constants
MINIMAL_DEADBAND = 0.1  # °C - for responsive heating
TARGET_REACHED_THRESHOLD = 0.1  # °C - when target is considered "reached"

# Temperature validation
MIN_VALID_TEMP = -20.0  # °C - minimum plausible temperature
MAX_VALID_TEMP = 50.0   # °C - maximum plausible temperature

# Fallback modes
FALLBACK_MODE_ZONE_AVERAGE = "zone_average"
FALLBACK_MODE_TRV = "trv"
FALLBACK_MODE_LAST_KNOWN = "last_known"

# Heating demand modes
HEATING_DEMAND_MODE_ANY_ROOM = "any_room"
HEATING_DEMAND_MODE_ZONE_AVERAGE = "zone_average"

# Attributes
ATTR_ZONE_ID = "zone_id"
ATTR_ROOM_ID = "room_id"
ATTR_BOOST_DURATION = "duration"
ATTR_BOOST_TEMP = "temperature"
ATTR_BOOST_END_TIME = "boost_end_time"
ATTR_CURRENT_SCHEDULE = "current_schedule"
ATTR_NEXT_SCHEDULE = "next_schedule"
ATTR_ROOM_TEMPS = "room_temperatures"
ATTR_AWAY_MODE = "away_mode"
ATTR_MODE = "mode"

# Services
SERVICE_SET_BOOST = "set_boost"
SERVICE_CLEAR_BOOST = "clear_boost"
SERVICE_SET_MODE = "set_mode"

# Storage
STORAGE_KEY = f"{DOMAIN}.storage"
STORAGE_VERSION = 1

# Update coordinator
UPDATE_INTERVAL = timedelta(seconds=DEFAULT_UPDATE_INTERVAL)
SENSOR_TIMEOUT = timedelta(minutes=DEFAULT_SENSOR_TIMEOUT)
