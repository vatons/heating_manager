"""Data models for Heating Manager."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SensorReading:
    """Individual sensor reading with validation status."""

    entity_id: str
    value: float | None
    last_seen: datetime | None
    last_seen_source: str | None  # "dedicated_sensor" or "state_last_updated"
    status: str  # "active", "timeout", "invalid", "unavailable"
    is_plausible: bool = True


@dataclass
class TemperatureReading:
    """Represents a single temperature reading with metadata."""

    value: float | None
    timestamp: datetime
    source: str  # "local_sensors", "zone_average", "unavailable"
    sensors_status: list[SensorReading] = field(default_factory=list)
    last_seen: datetime | None = None


@dataclass
class HeatingAnalyticsData:
    """What's learned about a room's heating, and estimates from it."""

    heating_rate: float | None          # °C/h once warming (None until enough stretches)
    warm_up_minutes: int | None         # minutes from heat starting to the room warming
    heating_stretches: int              # heated stretches learned from
    cooling_rate: float | None          # °C/h without heat (negative when cooling)
    heat_loss_per_degree: float | None  # °C/h lost per °C warmer than outside
    cooling_stretches: int              # stretches without heat learned from
    current_state: str | None           # "heated", "not_heated" or None
    current_minutes: int | None         # how long the current stretch has lasted
    rate_so_far: float | None           # °C/h in the current stretch
    trend: str  # "heating_rapidly", "heating_slowly", "stable", "cooling_slowly", "cooling_rapidly", "insufficient_data"
    eta_minutes: int | None             # minutes until the target is reached
    eta_timestamp: datetime | None
    confidence: float                   # 0.0-1.0
    logged_stretches: int = 0           # heated stretches in the room's log
    heating_power: float | None = None  # °C/h the radiators add before losses to outside
    expected_heating_rate: float | None = None  # °C/h at today's outdoor temperature
    overrun_rise: float | None = None   # °C a room keeps rising after heating stops
    overrun_minutes: int | None = None  # from heating stopping to the peak
    overrun_stretches: int = 0
    current_overrun: bool | None = None  # the current stretch is still in its overrun
    eta_status: str | None = None       # warming, cooling, still_warming, not_cooling, not_reaching, not_before_target_changes
    expected_peak: float | None = None  # still warming: the peak to expect
    expected_peak_minutes: int | None = None

    def to_dict(self) -> dict:
        """Convert to a dictionary for the room's data."""
        return {
            "heating_rate": self.heating_rate,
            "warm_up_minutes": self.warm_up_minutes,
            "heating_stretches": self.heating_stretches,
            "logged_stretches": self.logged_stretches,
            "cooling_rate": self.cooling_rate,
            "heat_loss_per_degree": self.heat_loss_per_degree,
            "cooling_stretches": self.cooling_stretches,
            "current_state": self.current_state,
            "current_minutes": self.current_minutes,
            "rate_so_far": self.rate_so_far,
            "trend": self.trend,
            "eta_minutes": self.eta_minutes,
            "eta_timestamp": self.eta_timestamp.isoformat() if self.eta_timestamp else None,
            "confidence": self.confidence,
            "heating_power": self.heating_power,
            "expected_heating_rate": self.expected_heating_rate,
            "overrun_rise": self.overrun_rise,
            "overrun_minutes": self.overrun_minutes,
            "overrun_stretches": self.overrun_stretches,
            "current_overrun": self.current_overrun,
            "eta_status": self.eta_status,
            "expected_peak": self.expected_peak,
            "expected_peak_minutes": self.expected_peak_minutes,
        }
