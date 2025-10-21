# Heating Manager for Home Assistant

A comprehensive Home Assistant custom component for managing multi-zone heating systems with schedules, room-level boost control, and intelligent temperature management.

## Features

- **Multi-Zone Management**: Organize your heating into logical zones (e.g., upstairs, downstairs)
- **Schedule-Based Control**: Define weekday and weekend heating schedules for each zone
- **Room-Level Boost**: Temporarily boost individual rooms without affecting the rest of the zone
- **Intelligent TRV Control**:
  - Self-learning offset compensation for TRV internal sensors
  - Exponential moving average (EMA) for efficient offset tracking
  - Adaptive heating boost based on room deficit
  - Automatic overshoot prevention and cooling
  - Per-TRV offset learning with persistent storage
- **Advanced Sensor Handling**:
  - Average multiple temperature sensors per room
  - Optional dedicated last_seen sensors for accurate timestamps
  - Automatic fallback when sensors go offline
  - Configurable sensor timeout (default: 15 minutes)
  - Mixed sensor format support (simple and extended)
- **Smart Heating Logic**:
  - Intelligent deadband prevents short-cycling while ensuring responsiveness
  - Configurable heating demand modes (any_room or zone_average)
  - Zone and global heating demand sensors for boiler control
- **Flexible Climate Entities**:
  - Room, Zone, and Global climate entities
  - Structured attributes with grouped data (boost, temperature, config, TRV control)
  - Full schedule visibility in zone attributes (current and next periods)
- **Away Mode**: Set all zones to frost protection temperature
- **Persistent State**: Boost timers, learned offsets, and settings survive restarts

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL and select "Integration" as the category
6. Click "Install"
7. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/heating_manager` directory to your Home Assistant's `custom_components` directory
2. Restart Home Assistant

## Configuration

### 1. Create Your Heating Configuration File

Create a file named `heating_manager.yaml` in your Home Assistant configuration directory:

```yaml
# heating_manager.yaml
update_interval: 60  # seconds between logic updates
fallback_mode: "zone_average"  # or "trv" / "last_known"
minimum_temp: 15  # Default temperature when no schedule is active (°C)
frost_protection_temp: 15  # Temperature for away mode (°C)
heating_demand_mode: "any_room"  # "any_room" or "zone_average" - How to calculate zone heating demand
heating_deadband: 0.3  # Temperature difference before stopping heating (°C) - prevents short cycling

# TRV Intelligent Control (compensates for TRV internal sensors being near radiator)
trv_overshoot_enabled: true  # Enable intelligent TRV setpoint calculation using sensor offset
trv_overshoot_max: 5.0  # Maximum boost above learned offset (°C) - increase if rooms heat slowly
trv_overshoot_threshold: 0.3  # Temperature above target to trigger cooling (°C)
trv_cooldown_offset: 1.0  # Temperature below target to set for faster cooling (°C)
trv_offset_ema_alpha: 0.15  # EMA smoothing factor (0.1=stable, 0.2=responsive)

zones:
  downstairs:
    name: "Downstairs Zone"
    schedule:
      weekday:
        - { start: "06:00", end: "09:00", temperature: 21 }
        - { start: "17:00", end: "22:00", temperature: 20 }
      weekend:
        - { start: "07:00", end: "23:00", temperature: 21 }
    rooms:
      living_room:
        name: "Living Room"
        trvs:
          - climate.living_room_trv
        sensors:
          - sensor.living_room_temp
          # Or with optional last_seen sensor:
          # - temperature: sensor.living_room_temp
          #   last_seen: sensor.living_room_last_seen
      kitchen:
        name: "Kitchen"
        trvs:
          - climate.kitchen_trv
        sensors:
          - sensor.kitchen_temp
          - sensor.kitchen_secondary_temp

  upstairs:
    name: "Upstairs Zone"
    schedule:
      weekday:
        - { start: "06:30", end: "08:30", temperature: 20 }
        - { start: "18:00", end: "22:30", temperature: 19.5 }
      weekend:
        - { start: "07:00", end: "23:00", temperature: 20 }
    rooms:
      bedroom:
        name: "Bedroom"
        trvs:
          - climate.bedroom_trv
        sensors:
          - sensor.bedroom_temp
      office:
        name: "Office"
        trvs:
          - climate.office_trv
        sensors:
          - sensor.office_temp
```

### 2. Add to configuration.yaml

Add the following to your `configuration.yaml`:

```yaml
heating_manager:
  config_file: heating_manager.yaml
```

**Note:** All configuration options (`update_interval`, `minimum_temp`, `frost_protection_temp`, `fallback_mode`) should now be set in `heating_manager.yaml` (shown above). You can optionally override them in `configuration.yaml` for backward compatibility, but the recommended approach is to keep all settings in `heating_manager.yaml`.

### 3. Restart Home Assistant

After restarting, the integration will create the following entities:

**Global:**
- `binary_sensor.hm_global_heating_demand` - Combined heating demand (OR of all zones)

**Per Zone:**
- `climate.hm_<zone_id>` - Zone climate control with manual temperature adjustment
- `binary_sensor.hm_<zone_id>_heating_demand` - Zone heating demand indicator (on = heat required)

**Per Room:**
- `climate.hm_<room_id>` - Room climate control entity

  Structured attributes for better organization:
  - **Identification**:
    - `zone_id`, `zone_name`, `room_id`
  - **Heating Status**:
    - `needs_heating` - Boolean indicating if heating is required
    - `away_mode` - Current away mode status
  - **Boost** (grouped object):
    - `temperature` - Boost target temperature (null if not active)
    - `end_time` - When boost will end (ISO format)
    - `duration_minutes` - Total boost duration
    - `time_remaining_minutes` - Minutes remaining
  - **Sensors** (array of sensor status objects):
    - Each sensor includes: `entity_id`, `value`, `last_seen`, `last_seen_source`, `status`
    - Status values: `"active"`, `"timeout"`, `"unavailable"`, `"invalid"`
  - **TRV Control** (grouped object, only present if TRVs exist):
    - `enabled` - Whether intelligent control is active
    - `trvs` - Array of TRV offset data with `entity_id`, `internal_temp`, `current_offset`, `learned_offset`, `setpoint`

## Usage

### Zone Climate Control

Each zone has a climate entity with the following features:

- **HVAC Modes**: Heat, Off
- **Preset Modes**:
  - `schedule` - Follow the configured schedule
  - `away` - Frost protection mode
  - `manual` - Manual control or boost active

### Room Boost

Enable boost for a room using the `heating_manager.set_boost` service:

```yaml
service: heating_manager.set_boost
data:
  zone_id: zone_1
  room_id: living_room
  duration: 60  # minutes (optional, default: 30)
  temperature: 22  # °C (optional, default: current room temp + 2°C)
```

Clear boost:

```yaml
service: heating_manager.clear_boost
data:
  zone_id: zone_1
  room_id: living_room
```

**Check boost status:** All boost information is available in the room climate entity attributes:
- `climate.living_room_hm` → attributes → `boost.temperature`, `boost.time_remaining_minutes`, etc.

### Heating Mode

Set the heating mode for all zones using the `heating_manager.set_mode` service:

**Schedule Mode** - Follow the configured heating schedules:

```yaml
service: heating_manager.set_mode
data:
  mode: schedule
```

**Away Mode** - Enable frost protection for all zones:

```yaml
service: heating_manager.set_mode
data:
  mode: away
```

### Manual Override Control

If you want the system to ignore manual TRV adjustments (like a child lock):

```yaml
service: heating_manager.ignore_manual_override
data:
  zone_id: downstairs
  room_id: living_room
  ignore: true  # true = ignore manual changes, false = respect them
```

### Heating Demand Sensor

Each zone has a binary sensor (`binary_sensor.hm_<zone_id>_heating_demand`) that indicates when heating is required. This is useful for:

- **Triggering boilers/heating systems** when any room in the zone needs heat
- **Controlling towel radiators** or other non-TRV radiators
- **Monitoring heating activity** in automations

#### Heating Demand Modes

You can configure how the zone heating demand is calculated using the `heating_demand_mode` setting:

**`any_room` (default)** - Zone demand is ON if **any single room** needs heating:
- More responsive - heats as soon as one room gets cold
- Better for ensuring all rooms stay comfortable
- May use more energy if rooms have different heating needs

**`zone_average`** - Zone demand is ON if the **average zone temperature** is below the **average target temperature**:
- More energy efficient - only heats when the overall zone is cold
- Better for zones where rooms naturally vary in temperature
- Prevents heating the whole zone for one cold room

**Configuration:**

```yaml
# Global default (applies to all zones)
heating_demand_mode: "any_room"

zones:
  downstairs:
    name: "Downstairs"
    heating_demand_mode: "zone_average"  # Override for this zone only
    schedule: ...
```

The sensor is **ON** based on the configured mode:
- **any_room**: Any room temperature < target - deadband (smart logic applied per room), OR any room has active boost
- **zone_average**: Average room temp < average target temp - deadband

**Example: Control Boiler Based on Zone Demand**

```yaml
automation:
  - alias: "Turn on Downstairs Heating"
    trigger:
      - platform: state
        entity_id: binary_sensor.hm_zone_1_heating_demand
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.boiler_zone_1

  - alias: "Turn off Downstairs Heating"
    trigger:
      - platform: state
        entity_id: binary_sensor.hm_zone_1_heating_demand
        to: "off"
        for: "00:05:00"  # 5 minute delay to prevent short cycling
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.boiler_zone_1
```

The sensor attributes include:
- `rooms_needing_heat` - List of room IDs requiring heating
- `boosted_rooms` - List of room IDs with active boost
- `away_mode` - Current away mode status

### Global Heating Demand

The integration also provides a global heating demand sensor (`binary_sensor.hm_global_heating_demand`) that combines ALL zones using OR logic. This sensor is **ON** when ANY zone requires heating.

This is particularly useful for:
- **Controlling a main boiler** that serves multiple zones
- **Simplified automation** when you want a single trigger for any heating activity
- **Monitoring overall heating status** across the entire system

**Example: Control Main Boiler Based on Global Demand**

```yaml
automation:
  - alias: "Turn on Main Boiler"
    trigger:
      - platform: state
        entity_id: binary_sensor.hm_global_heating_demand
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.main_boiler

  - alias: "Turn off Main Boiler"
    trigger:
      - platform: state
        entity_id: binary_sensor.hm_global_heating_demand
        to: "off"
        for: "00:05:00"  # 5 minute delay to prevent short cycling
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.main_boiler
```

The global sensor attributes include:
- `zones_needing_heat` - List of zone IDs requiring heating
- `rooms_needing_heat` - List of "zone_id/room_id" pairs requiring heating
- `boosted_rooms` - List of "zone_id/room_id" pairs with active boost
- `total_zones` - Total number of configured zones
- `zones_demanding_count` - Number of zones currently demanding heat
- `away_mode` - Current away mode status

**Flexibility:** You can choose to control zones individually using per-zone sensors, or use the global sensor for centralized boiler control. Both approaches work simultaneously.

## Temperature Logic

### Room Temperature Calculation

1. **Multiple sensors**: Average all valid sensors
2. **Sensor timeout**: If a sensor hasn't updated in 15 minutes, exclude it
3. **Single sensor timeout**: Fall back to other sensors in the room
4. **All sensors timeout**: Fall back to zone average
5. **No sensors**: Room boost is disabled

### Temperature Sensor Configuration

Sensors can be configured in two formats:

**Simple format** (uses Home Assistant's state.last_updated):
```yaml
sensors:
  - sensor.living_room_temp
  - sensor.kitchen_temp
```

**Extended format** (with optional dedicated last_seen sensor):
```yaml
sensors:
  - temperature: sensor.living_room_temp
    last_seen: sensor.living_room_last_seen  # Optional: ISO datetime sensor
  - temperature: sensor.kitchen_temp  # No last_seen, uses state.last_updated
```

**Benefits of dedicated last_seen sensors:**
- More accurate timestamps for battery-powered sensors
- Reflects when sensor actually measured (not when HA received update)
- Better timeout detection for sensors with irregular update patterns

**Last seen sensor format:** ISO 8601 datetime string: `YYYY-MM-DDTHH:MM:SS+00:00`

The system automatically uses the most accurate timestamp available and indicates the source in sensor attributes (`last_seen_source`: `"dedicated_sensor"` or `"state_last_updated"`).

### Boost Behavior

- Default boost: +2°C above current room temperature for 30 minutes
- Boost is independent per room - other rooms in the zone continue following schedule
- Boost state persists across Home Assistant restarts
- When boost expires, room returns to scheduled temperature

### Manual Override Detection

- Detects when TRV target temperature differs from system target by more than 0.5°C
- Can be configured per-room to ignore manual changes
- Useful for preventing children or guests from adjusting thermostats

## Example Automations

### Boost Living Room When Arriving Home

```yaml
automation:
  - alias: "Boost Living Room on Arrival"
    trigger:
      - platform: state
        entity_id: person.john
        to: "home"
    action:
      - service: heating_manager.set_boost
        data:
          zone_id: downstairs
          room_id: living_room
          duration: 120
          temperature: 22
```

### Enable Away Mode When Everyone Leaves

```yaml
automation:
  - alias: "Away Mode When Empty"
    trigger:
      - platform: state
        entity_id: group.all_persons
        to: "not_home"
        for: "00:30:00"
    action:
      - service: heating_manager.set_mode
        data:
          mode: away

  - alias: "Schedule Mode When Someone Arrives"
    trigger:
      - platform: state
        entity_id: group.all_persons
        to: "home"
    action:
      - service: heating_manager.set_mode
        data:
          mode: schedule
```

### Morning Bedroom Boost on Workdays

```yaml
automation:
  - alias: "Morning Bedroom Boost"
    trigger:
      - platform: time
        at: "06:00:00"
    condition:
      - condition: time
        weekday:
          - mon
          - tue
          - wed
          - thu
          - fri
    action:
      - service: heating_manager.set_boost
        data:
          zone_id: zone_2
          room_id: bedroom_1
          duration: 30
```

### Show Boost Status in UI

Create a template sensor to display boost status:

```yaml
template:
  - sensor:
      - name: "Living Room Boost Status"
        state: >
          {% if state_attr('climate.living_room_hm', 'boost')['temperature'] %}
            Boost active: {{ state_attr('climate.living_room_hm', 'boost')['time_remaining_minutes'] }} min remaining
          {% else %}
            Not boosted
          {% endif %}
```

## Troubleshooting

### Boost Not Working

- Check that the room has temperature sensors configured
- Verify sensors are updating (check state in Developer Tools)
- Check logs for errors: `Settings > System > Logs`

### TRVs Not Responding

- Ensure TRV entity IDs in config match actual entities
- Check TRV is online and responding to Home Assistant
- Verify the climate integration for your TRVs supports `set_temperature` service

### Temperature Readings Incorrect

- Check which sensors are being used: see room temperature sensor attributes
- Verify sensor update frequency (should be < 15 minutes)
- Check fallback mode in configuration

## Advanced Configuration

### Smart Heating Deadband

The `heating_deadband` parameter controls how much the temperature must drop below the target before heating is demanded. The system uses intelligent logic to balance responsiveness with energy efficiency:

**Smart Deadband Logic:**
- **When schedule changes to a new target**: Uses minimal deadband (0.1°C) for immediate response
- **When heating toward target**: Uses minimal deadband (0.1°C) until target is reached
- **When maintaining reached target**: Uses full configured deadband (default 0.3°C) to prevent short-cycling

**Example Scenarios:**

1. **Schedule changes from 18°C to 19.5°C, room is at 19.1°C**
   - Target just changed → Uses 0.1°C deadband
   - Room will heat immediately to reach new target

2. **Room has reached 19.5°C and cools to 19.2°C**
   - Target reached → Uses full 0.3°C deadband
   - Room will heat when it falls below 19.2°C (19.5 - 0.3)

3. **Room at 19.4°C, maintaining 19.5°C target**
   - Target reached, within deadband → No heating
   - Prevents cycling for small fluctuations

**Configuration:**
```yaml
heating_deadband: 0.3  # Default: 0.3°C, adjust based on your system
```

Lower values (0.2°C) = More precise temperature control, more frequent cycling
Higher values (0.5°C) = Less cycling, more temperature variation

### Intelligent TRV Control (Self-Learning Offset Compensation)

**Problem:** Many TRVs have internal temperature sensors mounted directly on the radiator, causing them to read significantly warmer than the actual room temperature. This leads to slow or inadequate heating.

**Solution:** The integration uses **intelligent offset tracking** to compensate for this bias automatically.

#### How It Works

1. **Reads both sensors**: External room sensor + TRV's internal sensor
2. **Calculates offset**: `offset = trv_internal_temp - room_temp`
3. **Learns over time**: Tracks offset history to understand typical behavior
4. **Adjusts TRV setpoint**: Sets TRV higher to compensate for warm internal sensor
5. **Adapts dynamically**: Adjusts as radiator heats/cools and offset changes

#### Example Scenario

**Without intelligent control:**
- Room sensor: 16°C (actual room temperature)
- TRV internal sensor: 22°C (warm from radiator)
- Target: 20°C
- TRV set to: 20°C
- **Problem**: TRV thinks room is at 22°C, barely heats, room never reaches 20°C ❌

**With intelligent control:**
- Room sensor: 16°C
- TRV internal sensor: 22°C
- Calculated offset: 6°C (22 - 16)
- Target: 20°C
- Deficit: 4°C (20 - 16) → Large deficit
- Adaptive boost: 5°C (maximum)
- **TRV set to**: 20 + 6 + 5 = **31°C** (capped at 30°C max)
- **Result**: TRV thinks it needs to heat from 22°C to 30°C (8°C jump), heats aggressively ✅
- Room quickly reaches 20°C target ✅

#### Adaptive Heating Logic

The system intelligently adjusts the boost based on how far the room is from target:

| Room Deficit | Boost Added | Example (target 20°C) |
|-------------|-------------|----------------------|
| > 3°C (very cold) | Maximum (5°C) | Room 16°C → TRV 31°C* |
| 1.5-3°C (cold) | Proportional (deficit × 1.5) | Room 18°C → TRV 29°C* |
| 0.5-1.5°C (approaching) | Moderate (1.5°C) | Room 19°C → TRV 27°C* |
| < 0.5°C (nearly there) | Minimal (0.5°C) | Room 19.7°C → TRV 26°C* |
| At target | Maintain (0.5°C) | Room 20°C → TRV 26°C* |

*Assuming 6°C learned offset. Actual values depend on your TRV and radiator configuration.

#### Self-Learning Behavior (Exponential Moving Average)

The system uses an **Exponential Moving Average (EMA)** for efficient, adaptive offset learning:

- **First reading**: Initializes EMA with current offset
- **Subsequent readings**: `EMA_new = α × current_offset + (1 - α) × EMA_old`
- **Smoothing factor (α)**: Configurable (default 0.15)
  - Lower α (0.10) = More stable, slower adaptation
  - Higher α (0.20) = More responsive, faster adaptation
- **Memory efficient**: Stores single float per TRV instead of array of readings
- **Persistent**: Learned EMAs saved and restored across restarts
- **Per-TRV**: Each TRV learns its own offset characteristics
- **Automatic migration**: Old list-based history automatically converted to EMA on upgrade

#### Configuration

```yaml
# TRV Intelligent Control
trv_overshoot_enabled: true        # Enable/disable intelligent control (default: true)
trv_overshoot_max: 5.0             # Maximum boost above offset (°C) (default: 5.0)
trv_overshoot_threshold: 0.3       # Temp above target to trigger cooling (default: 0.3)
trv_cooldown_offset: 1.0           # Temp below target for faster cooling (default: 1.0)
trv_offset_ema_alpha: 0.15         # EMA smoothing factor (default: 0.15)
```

**Tuning Guide:**
- **Rooms heat too slowly?** Increase `trv_overshoot_max` to 6-7°C
- **Rooms overshoot target?**
  - Reduce `trv_overshoot_max` to 3-4°C
  - Decrease `trv_overshoot_threshold` to 0.2°C for earlier cooling
  - Increase `trv_cooldown_offset` to 1.5°C for more aggressive cooling
- **Offset learning too reactive?** Decrease `trv_offset_ema_alpha` to 0.10
- **Offset learning too slow?** Increase `trv_offset_ema_alpha` to 0.20
- **Disable for specific setup?** Set `trv_overshoot_enabled: false`

#### Benefits

✅ **Automatic**: No manual tuning required - learns optimal behavior
✅ **Room-specific**: Each TRV/room combination learns its own offset
✅ **Fast heating**: Rooms reach target much faster than with naive control
✅ **Prevents overshoot**: Reduces boost as room approaches target
✅ **Energy efficient**: Only heats as much as needed
✅ **Self-adapting**: Adjusts to changing conditions (radiator size, room airflow, etc.)

**Note**: This feature requires TRVs that expose their internal `current_temperature` attribute to Home Assistant. Most modern smart TRVs support this.

### Heating Demand Modes

Control how zone heating demand is calculated:

- **`any_room`** (default): Zone heating triggers when any room is below target
  - Best for: Ensuring every room stays comfortable
  - Trade-off: May use more energy

- **`zone_average`**: Zone heating triggers when average zone temp is below average target
  - Best for: Energy efficiency, zones with natural temperature variation
  - Trade-off: Individual cold rooms may not trigger heating immediately

Can be set globally or overridden per-zone in `heating_manager.yaml`.

### Fallback Modes

- `zone_average`: Use average of all zone sensors (recommended)
- `trv`: Use TRV's internal temperature sensor
- `last_known`: Use last known sensor value

### Schedule Time Format

- Use 24-hour format: "HH:MM"
- Ensure times don't overlap
- When no schedule is active, uses `minimum_temp` setting

## Support

For issues, feature requests, or contributions:
- GitHub Issues: [Your Repository URL]
- Home Assistant Community: [Your Forum Thread URL]

## License

MIT License - See LICENSE file for details
