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
    """Analytics data for a room's heating performance."""

    heating_rate: float | None  # °C per hour (positive = heating, negative = cooling)
    cooling_rate: float | None  # °C per hour when not heating
    eta_minutes: int | None  # Estimated minutes to reach target
    eta_timestamp: datetime | None  # When target will be reached
    confidence: float  # 0.0-1.0 confidence in prediction
    samples_count: int  # Number of samples used for calculation
    trend: str  # "heating_rapidly", "heating_slowly", "stable", "cooling_slowly", "cooling_rapidly", "insufficient_data"

    def to_dict(self) -> dict:
        """Convert to dictionary for storage/transmission."""
        return {
            "heating_rate": self.heating_rate,
            "cooling_rate": self.cooling_rate,
            "eta_minutes": self.eta_minutes,
            "eta_timestamp": self.eta_timestamp.isoformat() if self.eta_timestamp else None,
            "confidence": self.confidence,
            "samples_count": self.samples_count,
            "trend": self.trend,
        }


@dataclass
class TemperatureHistoryEntry:
    """Single entry in temperature history for analytics."""

    timestamp: datetime
    temperature: float
    needs_heating: bool
    zone_heating_active: bool = False  # Is the zone's boiler/heat pump actually running

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "temperature": self.temperature,
            "needs_heating": self.needs_heating,
            "zone_heating_active": self.zone_heating_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TemperatureHistoryEntry":
        """Create from dictionary (for restoration from storage)."""
        from homeassistant.util import dt as dt_util

        return cls(
            timestamp=dt_util.parse_datetime(data["timestamp"]),
            temperature=data["temperature"],
            needs_heating=data["needs_heating"],
            zone_heating_active=data.get("zone_heating_active", False),  # Backwards compatible
        )
