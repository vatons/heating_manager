"""Heating analytics for tracking temperature changes and estimating heating performance."""
from collections import deque
from datetime import datetime, timedelta
import logging
from statistics import mean, stdev

from homeassistant.util import dt as dt_util

from .models import HeatingAnalyticsData, TemperatureHistoryEntry

_LOGGER = logging.getLogger(__name__)


class HeatingAnalytics:
    """Tracks temperature history and calculates heating performance metrics."""

    def __init__(self, history_size: int, min_samples: int, smoothing: float):
        """Initialize the heating analytics manager.

        Args:
            history_size: Maximum number of temperature readings to keep per room
            min_samples: Minimum number of samples required for derivative calculation
            smoothing: EMA smoothing factor for derivative (0.0-1.0, higher=more responsive)
        """
        self.history_size = history_size
        self.min_samples = min_samples
        self.smoothing = smoothing

        # Store: zone_id -> room_id -> deque of TemperatureHistoryEntry
        self.temp_history: dict[str, dict[str, deque]] = {}

        # Store smoothed derivatives: zone_id -> room_id -> {heating_rate, cooling_rate}
        self.smoothed_rates: dict[str, dict[str, dict]] = {}

        # Track previous state for intelligent recording decisions
        # zone_id -> room_id -> {last_temp, last_zone_heating, last_timestamp}
        self.last_state: dict[str, dict[str, dict]] = {}

        # Minimum temperature change to consider recording (avoids noise)
        self.min_temp_change = 0.05  # 0.05°C

    def record_temperature(
        self,
        zone_id: str,
        room_id: str,
        temp: float,
        needs_heating: bool,
        zone_heating_active: bool,
        timestamp: datetime | None = None,
    ) -> None:
        """Record a temperature reading for history tracking with state-aware logic.

        Only records when:
        - Zone heating is active AND temperature is changing
        - Zone heating state transitions occur
        - Significant temperature changes occur

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            temp: Temperature reading
            needs_heating: Whether the room currently needs heating
            zone_heating_active: Whether the zone's boiler/heat pump is actually running
            timestamp: Timestamp of reading (defaults to now)
        """
        if timestamp is None:
            timestamp = dt_util.now()

        # Initialize storage if needed
        if zone_id not in self.temp_history:
            self.temp_history[zone_id] = {}
        if room_id not in self.temp_history[zone_id]:
            self.temp_history[zone_id][room_id] = deque(maxlen=self.history_size)

        if zone_id not in self.last_state:
            self.last_state[zone_id] = {}
        if room_id not in self.last_state[zone_id]:
            self.last_state[zone_id][room_id] = {}

        last_state = self.last_state[zone_id][room_id]
        last_temp = last_state.get("last_temp")
        last_zone_heating = last_state.get("last_zone_heating")
        last_timestamp = last_state.get("last_timestamp")

        # Decide if we should record this sample
        should_record = False
        record_reason = ""

        # Always record first sample
        if last_temp is None:
            should_record = True
            record_reason = "initial"
        else:
            temp_change = abs(temp - last_temp)

            # Record on zone heating state transition
            if last_zone_heating is not None and zone_heating_active != last_zone_heating:
                should_record = True
                record_reason = "state_transition"

            # Record when zone is heating and temperature is rising significantly
            elif zone_heating_active and temp_change >= self.min_temp_change:
                if temp > last_temp:  # Temperature rising
                    should_record = True
                    record_reason = "heating_active"
                elif temp < last_temp:
                    # Temperature falling while heating - could indicate problem or sensor noise
                    # Record to capture potential issues
                    should_record = True
                    record_reason = "heating_temp_drop"

            # Record when zone is NOT heating and temperature is falling significantly
            elif not zone_heating_active and temp_change >= self.min_temp_change:
                if temp < last_temp:  # Temperature falling
                    should_record = True
                    record_reason = "cooling_active"
                elif temp > last_temp:
                    # Temperature rising while not heating - external heat source or sensor noise
                    should_record = True
                    record_reason = "cooling_temp_rise"

            # Record periodically even if idle to maintain some baseline (every ~10 minutes)
            elif last_timestamp is not None:
                time_since_last = (timestamp - last_timestamp).total_seconds()
                if time_since_last > 600:  # 10 minutes
                    should_record = True
                    record_reason = "periodic_baseline"

        # Update last state
        self.last_state[zone_id][room_id] = {
            "last_temp": temp,
            "last_zone_heating": zone_heating_active,
            "last_timestamp": timestamp,
        }

        # Record if criteria met
        if should_record:
            entry = TemperatureHistoryEntry(
                timestamp=timestamp,
                temperature=temp,
                needs_heating=needs_heating,
                zone_heating_active=zone_heating_active,
            )
            self.temp_history[zone_id][room_id].append(entry)

            _LOGGER.debug(
                "Recorded temperature for %s/%s: %.1f°C (zone_heating=%s, reason=%s)",
                zone_id,
                room_id,
                temp,
                zone_heating_active,
                record_reason,
            )
        else:
            _LOGGER.debug(
                "Skipped recording for %s/%s: %.1f°C (no significant change)",
                zone_id,
                room_id,
                temp,
            )

    def _calculate_derivative(
        self, zone_id: str, room_id: str, heating_filter: bool | None
    ) -> float | None:
        """Calculate temperature derivative (rate of change) in °C/hour.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            heating_filter: If True, only use readings when zone_heating_active=True
                          If False, only use readings when zone_heating_active=False
                          If None, use all readings

        Returns:
            Temperature change rate in °C/hour, or None if insufficient data
        """
        if zone_id not in self.temp_history or room_id not in self.temp_history[zone_id]:
            return None

        history = self.temp_history[zone_id][room_id]

        # Filter history based on zone heating state (not room needs_heating)
        if heating_filter is not None:
            filtered_history = [e for e in history if e.zone_heating_active == heating_filter]
        else:
            filtered_history = list(history)

        if len(filtered_history) < self.min_samples:
            return None

        # Calculate derivatives between consecutive points
        derivatives = []
        for i in range(1, len(filtered_history)):
            prev_entry = filtered_history[i - 1]
            curr_entry = filtered_history[i]

            time_diff_hours = (curr_entry.timestamp - prev_entry.timestamp).total_seconds() / 3600.0
            if time_diff_hours > 0:
                temp_diff = curr_entry.temperature - prev_entry.temperature
                derivative = temp_diff / time_diff_hours
                derivatives.append(derivative)

        if not derivatives:
            return None

        # Filter outliers (values beyond 2 standard deviations) if we have enough samples
        if len(derivatives) >= 5:
            try:
                avg = mean(derivatives)
                std = stdev(derivatives)
                derivatives = [d for d in derivatives if abs(d - avg) <= 2 * std]
            except Exception as err:
                _LOGGER.debug("Could not filter outliers: %s", err)

        if not derivatives:
            return None

        # Return average derivative
        return mean(derivatives)

    def calculate_heating_rate(self, zone_id: str, room_id: str) -> float | None:
        """Calculate heating rate in °C/hour when actively heating.

        Returns:
            Heating rate in °C/hour, or None if insufficient data
        """
        return self._calculate_derivative(zone_id, room_id, heating_filter=True)

    def calculate_cooling_rate(self, zone_id: str, room_id: str) -> float | None:
        """Calculate cooling rate in °C/hour when not heating.

        Returns:
            Cooling rate in °C/hour (typically negative), or None if insufficient data
        """
        return self._calculate_derivative(zone_id, room_id, heating_filter=False)

    def _update_smoothed_rates(
        self, zone_id: str, room_id: str, heating_rate: float | None, cooling_rate: float | None
    ) -> tuple[float | None, float | None]:
        """Update smoothed rates using EMA.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            heating_rate: Current heating rate calculation
            cooling_rate: Current cooling rate calculation

        Returns:
            Tuple of (smoothed_heating_rate, smoothed_cooling_rate)
        """
        # Initialize storage if needed
        if zone_id not in self.smoothed_rates:
            self.smoothed_rates[zone_id] = {}
        if room_id not in self.smoothed_rates[zone_id]:
            self.smoothed_rates[zone_id][room_id] = {
                "heating_rate": None,
                "cooling_rate": None,
            }

        room_rates = self.smoothed_rates[zone_id][room_id]

        # Apply EMA smoothing
        if heating_rate is not None:
            if room_rates["heating_rate"] is None:
                room_rates["heating_rate"] = heating_rate
            else:
                room_rates["heating_rate"] = (
                    self.smoothing * heating_rate + (1 - self.smoothing) * room_rates["heating_rate"]
                )

        if cooling_rate is not None:
            if room_rates["cooling_rate"] is None:
                room_rates["cooling_rate"] = cooling_rate
            else:
                room_rates["cooling_rate"] = (
                    self.smoothing * cooling_rate + (1 - self.smoothing) * room_rates["cooling_rate"]
                )

        return room_rates["heating_rate"], room_rates["cooling_rate"]

    def _calculate_rate_variance(
        self, zone_id: str, room_id: str, heating_filter: bool | None
    ) -> float | None:
        """Calculate variance in temperature change rates.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            heating_filter: If True, only use heating readings; False for cooling; None for all

        Returns:
            Standard deviation of rates, or None if insufficient data
        """
        if zone_id not in self.temp_history or room_id not in self.temp_history[zone_id]:
            return None

        history = self.temp_history[zone_id][room_id]

        # Filter history based on zone heating state
        if heating_filter is not None:
            filtered_history = [e for e in history if e.zone_heating_active == heating_filter]
        else:
            filtered_history = list(history)

        if len(filtered_history) < self.min_samples:
            return None

        # Calculate derivatives
        derivatives = []
        for i in range(1, len(filtered_history)):
            prev_entry = filtered_history[i - 1]
            curr_entry = filtered_history[i]

            time_diff_hours = (curr_entry.timestamp - prev_entry.timestamp).total_seconds() / 3600.0
            if time_diff_hours > 0:
                temp_diff = curr_entry.temperature - prev_entry.temperature
                derivative = temp_diff / time_diff_hours
                derivatives.append(derivative)

        if len(derivatives) < 2:
            return None

        try:
            return stdev(derivatives)
        except Exception as err:
            _LOGGER.debug("Could not calculate variance: %s", err)
            return None

    def _get_trend_description(self, rate: float | None) -> str:
        """Get human-readable temperature trend description.

        Args:
            rate: Temperature change rate in °C/hour

        Returns:
            Trend description string
        """
        if rate is None:
            return "insufficient_data"
        elif rate > 1.0:
            return "heating_rapidly"
        elif rate > 0.2:
            return "heating_slowly"
        elif rate >= -0.2:
            return "stable"
        elif rate >= -1.0:
            return "cooling_slowly"
        else:
            return "cooling_rapidly"

    def estimate_time_to_target(
        self,
        zone_id: str,
        room_id: str,
        current_temp: float,
        target_temp: float,
        needs_heating: bool,
    ) -> tuple[int | None, datetime | None, float]:
        """Estimate time to reach target temperature with variance-based confidence.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            current_temp: Current room temperature
            target_temp: Target temperature
            needs_heating: Whether room currently needs heating

        Returns:
            Tuple of (eta_minutes, eta_timestamp, confidence)
            - eta_minutes: Minutes until target reached (None if can't estimate)
            - eta_timestamp: Datetime when target will be reached (None if can't estimate)
            - confidence: Prediction confidence 0.0-1.0 (accounts for sample count and variance)
        """
        # Get appropriate rate based on heating state
        if needs_heating:
            rate = self.smoothed_rates.get(zone_id, {}).get(room_id, {}).get("heating_rate")
            heating_filter = True
        else:
            rate = self.smoothed_rates.get(zone_id, {}).get(room_id, {}).get("cooling_rate")
            heating_filter = False

        if rate is None or abs(rate) < 0.05:
            # No rate data or rate too small to be useful
            return None, None, 0.0

        temp_diff = target_temp - current_temp

        # Check if we're moving toward or away from target
        if (temp_diff > 0 and rate <= 0) or (temp_diff < 0 and rate >= 0):
            # Moving away from target or not moving at all
            return None, None, 0.0

        # Calculate time in hours
        time_hours = abs(temp_diff / rate)

        # Convert to minutes
        time_minutes = int(time_hours * 60)

        # Calculate ETA timestamp
        now = dt_util.now()
        eta_timestamp = now + timedelta(minutes=time_minutes)

        # Calculate base confidence from sample count
        history_count = len(self.temp_history.get(zone_id, {}).get(room_id, []))
        if history_count < self.min_samples:
            base_confidence = 0.0
        elif history_count < 10:
            base_confidence = 0.5
        elif history_count < 20:
            base_confidence = 0.75
        else:
            base_confidence = 0.9

        # Calculate variance factor (more variance = less confidence)
        variance = self._calculate_rate_variance(zone_id, room_id, heating_filter)
        if variance is not None and abs(rate) > 0:
            # Coefficient of variation: std_dev / mean
            # Higher CV = more variability = lower confidence
            cv = abs(variance / rate)
            if cv < 0.2:  # Very stable
                variance_factor = 1.0
            elif cv < 0.5:  # Moderately stable
                variance_factor = 0.9
            elif cv < 1.0:  # Some variability
                variance_factor = 0.7
            else:  # High variability
                variance_factor = 0.5
        else:
            variance_factor = 0.8  # Default moderate confidence if can't calculate

        # Combine factors
        confidence = base_confidence * variance_factor

        # Reduce confidence for very long predictions (>2 hours)
        if time_hours > 2:
            confidence *= 0.7

        # Ensure confidence is in valid range
        confidence = max(0.0, min(1.0, confidence))

        return time_minutes, eta_timestamp, confidence

    def get_analytics(
        self,
        zone_id: str,
        room_id: str,
        current_temp: float,
        target_temp: float,
        needs_heating: bool,
    ) -> HeatingAnalyticsData:
        """Get complete analytics for a room.

        Args:
            zone_id: Zone identifier
            room_id: Room identifier
            current_temp: Current room temperature
            target_temp: Target temperature
            needs_heating: Whether room currently needs heating

        Returns:
            HeatingAnalyticsData with all analytics information
        """
        # Calculate current rates
        heating_rate_raw = self.calculate_heating_rate(zone_id, room_id)
        cooling_rate_raw = self.calculate_cooling_rate(zone_id, room_id)

        # Update smoothed rates
        heating_rate, cooling_rate = self._update_smoothed_rates(
            zone_id, room_id, heating_rate_raw, cooling_rate_raw
        )

        # Get current rate for trend
        current_rate = heating_rate if needs_heating else cooling_rate
        trend = self._get_trend_description(current_rate)

        # Estimate time to target
        eta_minutes, eta_timestamp, confidence = self.estimate_time_to_target(
            zone_id, room_id, current_temp, target_temp, needs_heating
        )

        # Get sample count
        samples_count = len(self.temp_history.get(zone_id, {}).get(room_id, []))

        return HeatingAnalyticsData(
            heating_rate=heating_rate,
            cooling_rate=cooling_rate,
            eta_minutes=eta_minutes,
            eta_timestamp=eta_timestamp,
            confidence=confidence,
            samples_count=samples_count,
            trend=trend,
        )

    def get_history_for_storage(self) -> dict:
        """Get temperature history for persistence (last 10 entries per room).

        Returns:
            Dictionary suitable for JSON storage
        """
        storage_data = {}

        for zone_id, zones in self.temp_history.items():
            storage_data[zone_id] = {}
            for room_id, history in zones.items():
                # Keep only last 10 entries for storage efficiency
                recent_history = list(history)[-10:] if len(history) > 10 else list(history)
                storage_data[zone_id][room_id] = {
                    "history": [entry.to_dict() for entry in recent_history],
                    "smoothed_rates": self.smoothed_rates.get(zone_id, {}).get(room_id, {}),
                }

        return storage_data

    def restore_history(self, stored_data: dict) -> None:
        """Restore temperature history from storage.

        Args:
            stored_data: Dictionary from get_history_for_storage()
        """
        for zone_id, zones in stored_data.items():
            if zone_id not in self.temp_history:
                self.temp_history[zone_id] = {}
            if zone_id not in self.smoothed_rates:
                self.smoothed_rates[zone_id] = {}

            for room_id, room_data in zones.items():
                # Restore history
                self.temp_history[zone_id][room_id] = deque(maxlen=self.history_size)
                for entry_dict in room_data.get("history", []):
                    try:
                        entry = TemperatureHistoryEntry.from_dict(entry_dict)
                        self.temp_history[zone_id][room_id].append(entry)
                    except Exception as err:
                        _LOGGER.warning(
                            "Could not restore history entry for %s/%s: %s",
                            zone_id,
                            room_id,
                            err,
                        )

                # Restore smoothed rates
                self.smoothed_rates[zone_id][room_id] = room_data.get("smoothed_rates", {})

        _LOGGER.info("Restored temperature history for %d zones", len(stored_data))
