# Heating Manager for Home Assistant

A comprehensive Home Assistant custom component for managing multi-zone heating systems with schedules, room-level boost control, and intelligent temperature management.

## Features 

- **Multi-Zone Management**: Organize your heating into logical zones (e.g., upstairs, downstairs)
- **Schedule-Based Control**: Define weekday and weekend heating schedules for each zone
- **Room-Level Boost**: Temporarily boost individual rooms without affecting the rest of the zone
- **Per-Room Temperature Offsets**: Set individual temperature adjustments for specific rooms within a zone (e.g., keep bedroom 2°C cooler than the rest of the zone)
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
  - Configurable sensor timeout (default: 30 minutes)
  - Mixed sensor format support (simple and extended)
- **Smart Heating Logic**:
  - Intelligent deadband prevents short-cycling while ensuring responsiveness
  - Configurable heating demand modes (any_room or zone_average)
  - Zone and global heating demand sensors for boiler control
- **Flexible Climate Entities**:
  - Room, Zone, and Global climate entities
  - Structured attributes with grouped data (boost, temperature, config, TRV control)
  - Full schedule visibility in zone attributes (current and next periods)
- **Heating Analytics**:
  - Track heating and cooling rates per room
  - Estimate time to reach target temperature (ETA)
  - Trend analysis and confidence scoring
  - Persistent history across restarts
- **Away Mode**: Set all zones to frost protection temperature
- **Persistent State**: Boost timers, learned offsets, analytics history, and settings survive restarts

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

## Setup

Everything is set up in the Home Assistant UI; no YAML is needed.

1. Go to **Settings → Devices & services → Add integration** and choose **Heating Manager**.
2. Name your first zone (an area served by one boiler or heating circuit, e.g. "Downstairs"). It starts with a 06:30–22:00 schedule at 19°C.
3. Open the integration and click **Configure**. From the menu:
   - **Zones, rooms and schedules**: pick a zone (or add one), then
     - **Rooms**: add a room, pick its TRVs and temperature sensors, and optionally set a temperature offset
     - **Schedule**: choose weekdays or weekends, then add, edit or delete periods (start, end, temperature). "Replace with the weekday/weekend schedule" copies one to the other
     - **Name, demand mode and monitoring**: rename the zone, override the default demand mode for it, or make it **monitoring only**
   - **Settings**: the everyday options (temperature outside the schedule, away temperature, demand mode, sensor fallback, deadband, boost duration)
   - **Advanced settings**: TRV control tuning, analytics and update interval. The defaults suit most homes
4. Choose **Save and close** (available on the main menu, each zone's menu and every list) to apply your changes. Closing the dialog without saving discards them.

Each zone appears as a device, with its room entities grouped under it.

### Migrating from YAML

If you used an earlier version configured with `heating_manager:` in `configuration.yaml`, just upgrade and restart. Your `heating_manager.yaml` is imported into the UI automatically, keeping your zones, rooms, schedules and settings, as well as your entity IDs, history, boosts, overrides and learned TRV offsets.

If the automatic import didn't happen (for example, you removed the YAML before upgrading, or the integration was set up with no zones), use **Configure → Import from YAML file** instead. Enter the file's path relative to your config folder (e.g. `heating_manager.yaml`) and choose whether to also import settings. You'll see the zones, rooms and settings found before anything changes. Zones in the file are added, or replace a zone with the same ID; zones you created in the UI are kept. This works any time, not just when upgrading.

After the automatic import, Home Assistant shows a repair notice: remove the `heating_manager:` entry from `configuration.yaml`, delete `heating_manager.yaml` and restart. From then on, make changes with **Configure**; edits to the YAML are ignored.

### Entities


Entity IDs are based on names. Installs migrated from YAML keep their existing entity IDs.

**Global:**
- `climate.heating_manager` - Global climate entity showing combined heating demand across all zones

**Per Zone:**
- `climate.<zone name>` (e.g. `climate.downstairs`) - Zone climate entity with manual temperature adjustment and heating demand status

**Per Room:**
- `climate.<zone name>_<room name>` (e.g. `climate.downstairs_lounge`) - Room climate control entity

  Structured attributes for better organization:
  - **Identification**:
    - `zone_id`, `zone_name`, `room_id`
  - **Heating Status**:
    - `needs_heating` - Boolean indicating if heating is required
    - `away_mode` - Current away mode status
  - **Boost** (grouped object):
    - `boost.temperature` - Boost target temperature (null if not active)
    - `boost.end_time` - When boost will end (ISO format)
    - `boost.duration_minutes` - Total boost duration
    - `boost.time_remaining_minutes` - Minutes remaining
  - **Sensors** (array of sensor status objects):
    - Each sensor includes: `entity_id`, `value`, `last_seen`, `last_seen_source`, `status`
    - Status values: `"active"`, `"timeout"`, `"unavailable"`, `"invalid"`
  - **Manual Override** (grouped object):
    - `manual_override.active` - Boolean, true if a manual room override is set
    - `manual_override.temperature` - The manual override temperature (null if not active)
  - **TRV Control** (grouped object, only present if TRVs exist):
    - `enabled` - Whether intelligent control is active
    - `trvs` - Array of TRV offset data with `entity_id`, `internal_temp`, `current_offset`, `learned_offset`, `setpoint`

## Usage

### Zone Climate Control

Each zone has a climate entity with the following features:

- **HVAC Modes**: Heat, Off
- **Preset Modes**:
  - `schedule` - Follow the configured schedule (also clears any active boost or manual override)
  - `away` - Frost protection mode
  - `boost` - Temporarily boost room temperature above the schedule

### Room Boost

Enable boost for a room using the `heating_manager.set_boost` service:

```yaml
service: heating_manager.set_boost
data:
  zone_id: zone_1
  room_id: living_room
  duration: 60  # minutes (optional, default: 30)
  temperature: 22  # °C (optional, default: room's current target, or its temperature if higher, + 2°C)
```

Clear boost:

```yaml
service: heating_manager.clear_boost
data:
  zone_id: zone_1
  room_id: living_room
```

**Check boost status:** All boost information is available in the room climate entity attributes:
- `climate.downstairs_living_room` → attributes → `boost.temperature`, `boost.time_remaining_minutes`, etc.

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

### Heating Demand

Each zone climate entity (e.g. `climate.downstairs`) exposes heating demand via its `hvac_action` attribute (`heating` or `idle`) and the `heating_demand` state attribute. This is useful for:

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
- Uses the same smart deadband as rooms: after the schedule (or an override) raises the average target, the zone heats as soon as it's 0.1°C below it; once the target is reached, it waits until the average drops below target minus the heating deadband

**Configuration:** set the default in **Configure → Settings → Default heating demand mode**. To override it for one zone, use **Configure → Zones, rooms and schedules → (zone) → Name and heating demand mode**.

The zone's `hvac_action` is `heating` based on the configured mode:
- **any_room**: Any room temperature < target - deadband (smart logic applied per room)
- **zone_average**: Average room temp < average target temp - deadband

**Example: Control Boiler Based on Zone Demand**

```yaml
automation:
  - alias: "Turn on Downstairs Heating"
    trigger:
      - platform: state
        entity_id: climate.downstairs
        attribute: hvac_action
        to: "heating"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.boiler_zone_1

  - alias: "Turn off Downstairs Heating"
    trigger:
      - platform: state
        entity_id: climate.downstairs
        attribute: hvac_action
        to: "idle"
        for: "00:05:00"  # 5 minute delay to prevent short cycling
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.boiler_zone_1
```

The zone climate entity attributes include:
- `heating_demand` - Boolean, true when zone requires heating
- `rooms_needing_heat` - List of room IDs currently requiring heating
- `boost.active` - Boolean, true if any room in the zone has an active boost
- `boost.room_ids` - List of room IDs with active boost
- `manual_override.active` - Boolean, true if a zone-level manual override is set
- `manual_override.temperature` - The manual override temperature (null if not active)
- `away_mode` - Current away mode status
- `schedule.current_temperature` - Currently scheduled target temperature
- `schedule.current_period` - Current schedule period (`start`, `end`, `temperature`)
- `schedule.next_period` - Next schedule period

#### Boiler protection (minimum on/off times)

To stop the boiler short-cycling, set **Minimum boiler on time** and **Minimum boiler off time** in **Configure → Advanced settings** (minutes, 0 = off, the default). Once a zone starts calling for heat it keeps calling for at least the on time; once it stops, it waits at least the off time before calling again. The global entity is held the same way, so zones taking turns can't cycle a shared boiler. TRVs aren't held: a room that's warm enough still closes its TRV.

The zone entity shows `heating_demand_requested` (what the rooms want now) next to `heating_demand` (what's being signalled), and `demand_hold` (`min_on`, `min_off` or null) explains any difference. The global entity has `demand_hold` too.

#### Monitoring-only zones

A zone can be set to **monitoring only** (**Configure → Zones, rooms and schedules → (zone) → Name, demand mode and monitoring**). Use this for rooms you want to see but not heat, such as a bathroom or cloakroom with sensors but no controllable radiators. A monitoring-only zone:

- Still shows each room's temperature, target, sensor status and analytics
- Never calls for heat: its zone entity always reports `idle`, and it is left out of the global entity's demand and averages, so it can't switch the boiler on
- Never sends commands to TRVs in its rooms

### Global Heating Demand

The global climate entity (`climate.heating_manager`) combines ALL zones using OR logic. Its `hvac_action` is `heating` when ANY zone requires heating.

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
        entity_id: climate.heating_manager
        attribute: hvac_action
        to: "heating"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.main_boiler

  - alias: "Turn off Main Boiler"
    trigger:
      - platform: state
        entity_id: climate.heating_manager
        attribute: hvac_action
        to: "idle"
        for: "00:05:00"  # 5 minute delay to prevent short cycling
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.main_boiler
```

The global climate entity attributes include:
- `heating.zones_needing_heat` - List of zone IDs requiring heating
- `heating.rooms_needing_heat` - List of "zone_id/room_id" pairs requiring heating
- `boost.active` - Boolean, true if any room in any zone has an active boost
- `boost.room_ids` - List of "zone_id/room_id" pairs with active boost
- `total_zones` - Total number of configured zones
- `zones_demanding_heat` - Number of zones currently demanding heat
- `away_mode` - Current away mode status

**Flexibility:** You can choose to control zones individually using per-zone climate entities, or use the global entity for centralized boiler control. Both approaches work simultaneously.

## Temperature Logic

### Room Temperature Calculation

1. **Multiple sensors**: Average all valid sensors
2. **Sensor timeout**: If a sensor hasn't updated in 30 minutes, exclude it
3. **Single sensor timeout**: Fall back to other sensors in the room
4. **All sensors timeout**: Fall back to zone average
5. **No sensors**: Room boost is disabled

### Temperature Sensor Configuration

Pick a room's temperature sensors in its room form (**Configure → Zones, rooms and schedules → (zone) → Rooms → (room)**). With one or two fresh sensors the room uses their average; with three or more it uses the **median**, so a single odd sensor (one near a fridge, oven or window) can't skew the room. Sensors can report in °C or °F; readings are converted.

A sensor counts as fresh if it reported within the last 30 minutes, using Home Assistant's own report time, so a sensor holding a steady temperature is still fresh. For sensors that expose a separate timestamp entity (for example Zigbee2MQTT's `last_seen`), tick **Set 'last seen' sensors** in the room form to map each sensor to it.

**Benefits of dedicated last_seen sensors:**
- More accurate timestamps for battery-powered sensors
- Reflects when sensor actually measured (not when HA received update)

**Last seen sensor format:** ISO 8601 datetime string: `YYYY-MM-DDTHH:MM:SS+00:00`

The system automatically uses the most accurate timestamp available and indicates the source in sensor attributes (`last_seen_source`: `"dedicated_sensor"` or `"state_last_updated"`).

### Temperature Override Behaviour

**Manual room override** (dragging the temperature slider on a room climate entity):
- Sets a per-room manual temperature that overrides the schedule and any zone-level override
- Behaviour depends on whether boost is currently active for that room:
  - **No boost active**: any temperature other than the current scheduled temperature sets a manual room override; setting back to the scheduled temperature clears it immediately
  - **Boost active + new temp above schedule**: keeps boost running but updates its target temperature
  - **Boost active + new temp below schedule**: clears boost and sets a manual room override at the lower temperature
  - **Boost active + new temp equals schedule**: clears both boost and manual override (reverts to schedule)
- Persists until the schedule moves to a different temperature period, then automatically clears
- Switching preset back to `schedule` also clears the override

**Manual zone override** (dragging the temperature slider on a zone climate entity):
- Sets a zone-wide manual temperature that overrides the schedule for all rooms in the zone
- Per-room manual overrides are cleared when a zone override is set
- Persists until the schedule moves to a different temperature period, then automatically clears
- Switching preset back to `schedule` also clears the override

**Boost** (via the `boost` preset or `set_boost` service):
- Temporarily raises a specific room's temperature above the schedule
- Default: +2°C above the room's current target (or its current temperature, if higher) for 30 minutes
- Room-level — other rooms in the zone continue following the schedule
- Any existing manual room override for that room is cleared when boost activates
- Boost state persists across Home Assistant restarts
- When boost expires, room returns to the scheduled (or manual zone) temperature
- Switching preset back to `schedule` also clears any active boost

### Room Temperature Offsets

You can configure individual temperature adjustments for specific rooms within a zone using the `temperature_offset` parameter. This allows you to maintain different comfort levels in different rooms while keeping them all on the same heating schedule.

**Configuration:** set **Temperature offset** in the room's form (**Configure → Zones, rooms and schedules → (zone) → Rooms → (room)**).

**How it works:**

When the zone schedule sets a target temperature of 19.5°C:
- Bedroom with `temperature_offset: -2.0` will target **17.5°C** (19.5 - 2.0)
- Office with `temperature_offset: 1.5` will target **21.0°C** (19.5 + 1.5)
- Rooms without an offset will target **19.5°C**

**Common use cases:**
- **Cooler bedrooms**: Many people prefer sleeping in cooler rooms (`temperature_offset: -2.0`)
- **Warmer bathrooms**: Keep bathrooms slightly warmer for comfort (`temperature_offset: 1.0`)
- **Passive rooms**: Rooms that naturally stay warmer (south-facing, smaller) can be offset lower
- **Active rooms**: Home offices or living rooms can be offset higher during work hours

**Important notes:**
- The offset applies to targets the room inherits from its zone: the schedule and a zone-level manual temperature
- Targets set for the room itself are used exactly as set: a manual temperature set on the room's climate entity, and boost temperatures
- Away mode uses `frost_protection_temp` exactly, without the offset
- Offsets can be positive (warmer) or negative (cooler)
- The room's climate entity shows the offset target; setting the room back to that value clears its manual override
- Each room's heating demand and TRV control uses the offset-adjusted target

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
        target:
          entity_id: climate.downstairs_living_room
        data:
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
        target:
          entity_id: climate.upstairs_bedroom
        data:
          duration: 30
```

### Show Boost Status in UI

Create a template sensor to display boost status:

```yaml
template:
  - sensor:
      - name: "Living Room Boost Status"
        state: >
          {% if state_attr('climate.downstairs_living_room', 'boost')['temperature'] %}
            Boost active: {{ state_attr('climate.downstairs_living_room', 'boost')['time_remaining_minutes'] }} min remaining
          {% else %}
            Not boosted
          {% endif %}
```

## Troubleshooting

### Boost Not Working

- Check that the room has temperature sensors configured
- Verify sensors are updating (check state in Developer Tools)
- Check logs for errors: `Settings > System > Logs`

### Repair notice: "Heating Manager can't find some entities"

A TRV or sensor used by a room has been missing (renamed, deleted, or its integration didn't load) or disabled for 10 minutes. The notice lists each one with its room. Update the room in **Configure**, or re-enable/restore the entity; the notice clears by itself. Devices that are merely offline (state "unavailable") aren't reported.

### TRVs Not Responding

- Each TRV can belong to one room only. Configure rejects a TRV already used elsewhere; in an import, a TRV listed in several rooms is kept in the first and a warning is shown
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

**Configuration:** **Configure → Settings → Heating deadband** (default 0.3°C).

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

Set under **Configure → Advanced settings**:

| Setting | Former YAML key | Default |
|---|---|---|
| Smart TRV control | `trv_overshoot_enabled` | on |
| Maximum TRV boost | `trv_overshoot_max` | 5.0°C |
| Overshoot threshold | `trv_overshoot_threshold` | 0.3°C |
| Cooldown offset | `trv_cooldown_offset` | 1.0°C |
| TRV offset learning rate | `trv_offset_ema_alpha` | 0.15 |

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

### Heating Analytics

The integration includes an optional heating analytics system that tracks temperature history and calculates performance metrics for each room. This provides valuable insights into heating efficiency and helps estimate when rooms will reach their target temperatures.

**Features:**
- **Heating Rate**: Measures how quickly rooms heat up (°C per hour)
- **Cooling Rate**: Measures how quickly rooms cool down (°C per hour)
- **ETA Estimation**: Predicts when a room will reach its target temperature
- **Confidence Scoring**: Indicates reliability of predictions (0.0-1.0)
- **Trend Analysis**: Categorizes heating behavior (heating_rapidly, heating_slowly, stable, cooling_slowly, cooling_rapidly, insufficient_data)

**Configuration:** **Configure → Advanced settings**: Heating analytics (on), Analytics history size (30 readings), Analytics minimum samples (3) and Analytics smoothing (0.3).

**Analytics data is available in room climate entity attributes:**
```yaml
heating_analytics:
  heating_rate_per_hour: 1.8        # °C per hour when heating
  cooling_rate_per_hour: -0.4       # °C per hour when not heating
  estimated_time_to_target:
    minutes: 45                     # Estimated minutes to reach target
    timestamp: "2024-01-15T14:30:00+00:00"  # When target will be reached
    confidence_percent: 85          # Confidence in prediction (0-100)
  temperature_trend: "heating_rapidly"  # Current heating trend
  samples_count: 15                 # Number of samples used
```

**Use cases:**
- Monitor heating efficiency across different rooms
- Identify rooms that heat/cool unusually slowly (possible insulation issues)
- Build automations that pre-heat rooms before scheduled times
- Display estimated time to reach target in UI dashboards

**Disabling analytics:**
If you don't need this feature, turn off **Heating analytics** in **Configure → Advanced settings** to reduce storage and processing.

### Heating Demand Modes

Control how zone heating demand is calculated:

- **`any_room`** (default): Zone heating triggers when any room is below target
  - Best for: Ensuring every room stays comfortable
  - Trade-off: May use more energy

- **`zone_average`**: Zone heating triggers when average zone temp is below average target
  - Best for: Energy efficiency, zones with natural temperature variation
  - Trade-off: Individual cold rooms may not trigger heating immediately

Set the default in **Configure → Settings**; override it per zone in the zone's **Name and heating demand mode** screen.

### Fallback Modes

Set in **Configure → Settings → When a room's sensors are unavailable**. Used when a room's sensors have had no reading in the last 30 minutes:

- `zone_average`: Use the average of the zone's other fresh sensors (default, recommended)
- `trv`: Use the average internal temperature of the room's TRVs. These usually read warmer than the room
- `last_known`: Use the room's most recent sensor reading, however old

If `trv` or `last_known` has no data, the zone average is used. TRV offsets are only learned from the room's own sensors, never from a fallback temperature.

A room with TRVs but **no sensors configured** always uses its TRVs' own temperature (then the zone average), whatever this setting, so it still calls for heat on its own. TRV sensors usually read warmer than the room, so expect such rooms to run a little cool; a room sensor is better.

### Fahrenheit

Heating Manager works with Home Assistant set to °F. Sensor readings and TRV temperatures, limits and step sizes are converted automatically, and the room, zone and global climate entities display °F. Schedules, offsets and the temperatures in **Configure** are entered in °C. The `set_boost` service's `temperature` is in your unit system.

### Schedule Time Format

- Periods are edited with time pickers in **Configure → Zones, rooms and schedules → (zone) → Schedule**
- `end` is exclusive; use 00:00 to run until midnight
- A period with the same start and end (e.g. "00:00" to "00:00") covers the whole day
- A period ending before it starts (e.g. "22:00" to "06:00") spans midnight
- Periods on the same day can't overlap
- When no schedule is active, uses `minimum_temp` setting

## Development

### Running the tests

The test suite runs against a real Home Assistant core using
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component).
TRVs are simulated by a small in-test climate platform, so Home Assistant's own
`climate.set_temperature` validation is exercised as it is in production.

The tests target Home Assistant 2026.10, which requires Python 3.14. With
[uv](https://docs.astral.sh/uv/) (it downloads Python 3.14 if needed):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest
```

Known bugs are captured as `xfail(strict=True)` tests whose `reason` starts with
`BUG:`. List them with `.venv/bin/pytest -rx`. When a fix makes one of these tests
pass, pytest reports it as a failure (`XPASS(strict)`) until the `xfail` marker is
removed, so fixed bugs stay covered as regression tests.

## Support

For issues, feature requests, or contributions:
- GitHub Issues: [Your Repository URL]
- Home Assistant Community: [Your Forum Thread URL]

## License

MIT License - See LICENSE file for details
