"""Heating analytics: learn how each room heats and cools, from whole stretches of time.

A room's readings are split into stretches: "heated" while it wants heat and its
zone's heat source is on, "not heated" otherwise. When a stretch ends, what it
showed is folded into the room's long-term figures:

- warm-up: how long after heat starts the room begins to warm (radiators take
  time to get hot);
- heating rate: how fast it warms after that, fitted as a straight line through
  every reading, so a sensor that reports in 0.1 °C steps still gives the right
  answer;
- overrun: after heating stops, radiators (and what they warmed) keep heating the
  room for a while; how far it keeps rising and how long until it peaks;
- cooling rate: how fast it cools without heat, from the peak after heating (or
  straight away when it wasn't just heated);
- heat loss per degree: the cooling rate divided by how much warmer the room is
  than outside, when an outdoor temperature is available. Unlike the cooling
  rate it doesn't change with the weather.
- heating power: what the radiators alone would add, before the room's losses to
  outside (heating rate + heat loss x (room - outside)). Unlike the heating rate it
  doesn't change with the weather, so the warming estimate uses it, less the losses
  at today's outdoor temperature, once heat loss is known.

Only readings from the room's own sensors are used; fallback temperatures (zone
average, TRV) are skipped. Learned figures are saved; the stretch in progress
isn't.

Each heated stretch is also logged with what it started from: how long heat had
already been flowing (a cold boiler or one already hot), how many TRVs were taking
heat, how long since the room was last heated, the outdoor temperature and how far
the room had to climb. The log isn't used for estimates yet; it is the data for
warming a room up in time for a schedule change (see diagnostics). It is saved in
its own file, one compact line per stretch, and only when a stretch is added.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.util import dt as dt_util

from .models import HeatingAnalyticsData

_LOGGER = logging.getLogger(__name__)

STORAGE_FORMAT = 2

# Readings further apart than this (e.g. across a restart) end the stretch unlearned
MAX_SAMPLE_GAP = timedelta(minutes=30)
# A rise of one sensor step above the stretch's first reading: the room has started to warm
RISE_THRESHOLD = 0.1
# Heated stretches shorter than this aren't used for the warm-up time
MIN_HEATED_STRETCH = timedelta(minutes=15)
# The warming part of a heated stretch must be at least this long for a heating rate
MIN_RISE_SPAN = timedelta(minutes=10)
# After heating stops, radiators still give heat for a while (the overrun). It is over,
# and cooling is learned from the peak on, once the room has fallen RISE_THRESHOLD below
# its peak or gone OVERRUN_FLAT without a new high, and never sooner than COOL_SETTLE
# (a sensor can lag) or later than MAX_OVERRUN
COOL_SETTLE = timedelta(minutes=15)
OVERRUN_FLAT = timedelta(minutes=30)
MAX_OVERRUN = timedelta(hours=2)
# The cooling part of a stretch must be at least this long for a cooling rate
MIN_COOLING_SPAN = timedelta(minutes=30)
# A long stretch is learned from in parts this long, so a room that's never heated still learns
MAX_STRETCH_SPAN = timedelta(hours=3)
# Heat loss per degree needs the room this much warmer than outside to be meaningful
MIN_INDOOR_OUTDOOR_DIFFERENCE = 3.0
# The rate of the stretch in progress is shown once it covers this long
MIN_LIVE_SPAN = timedelta(minutes=5)
# A rate within this (°C/h) of zero is steady
STABLE_RATE = 0.05
# The rate during the overrun is shown from the readings this recent
OVERRUN_RATE_WINDOW = timedelta(minutes=20)
# Heated stretches kept in each room's log (a few weeks of heating)
LOG_SIZE = 200
LOG_FORMAT = 1
# The log's fields, in the order saved: each entry is stored as one line of
# comma-separated values with these columns
LOG_COLUMNS = (
    "start", "minutes", "continued", "start_temp", "end_temp", "target", "climb_needed",
    "outdoor", "warm_up_minutes", "rate", "zone_heat_minutes", "house_heat_minutes",
    "trvs_heating", "trvs_heating_mean", "minutes_since_heated",
    "overrun_rise", "overrun_minutes",
)
_LOG_TEXT = {"start"}
_LOG_BOOL = {"continued"}
_LOG_INT = {"trvs_heating"}


@dataclass
class _LineFit:
    """Running least-squares fit of temperature against time (hours since `origin`)."""

    origin: datetime
    n: int = 0
    sum_t: float = 0.0
    sum_y: float = 0.0
    sum_tt: float = 0.0
    sum_ty: float = 0.0
    first: datetime | None = None
    last: datetime | None = None
    sum_outdoor: float = 0.0
    n_outdoor: int = 0

    def add(self, timestamp: datetime, temperature: float, outdoor: float | None) -> None:
        t = (timestamp - self.origin).total_seconds() / 3600.0
        self.n += 1
        self.sum_t += t
        self.sum_y += temperature
        self.sum_tt += t * t
        self.sum_ty += t * temperature
        self.first = self.first or timestamp
        self.last = timestamp
        if outdoor is not None:
            self.sum_outdoor += outdoor
            self.n_outdoor += 1

    @property
    def span(self) -> timedelta:
        return (self.last - self.first) if self.first and self.last else timedelta(0)

    def slope(self) -> float | None:
        """°C per hour, or None with too little to fit."""
        if self.n < 2:
            return None
        denominator = self.n * self.sum_tt - self.sum_t * self.sum_t
        if denominator <= 1e-12:
            return None
        return (self.n * self.sum_ty - self.sum_t * self.sum_y) / denominator

    def time_at(self, temperature: float) -> datetime | None:
        """When the fitted line passes a temperature (None if it's flat or falling)."""
        slope = self.slope()
        if slope is None or slope <= 0:
            return None
        intercept = (self.sum_y - slope * self.sum_t) / self.n
        return self.origin + timedelta(hours=(temperature - intercept) / slope)

    @property
    def mean_temperature(self) -> float:
        return self.sum_y / self.n

    @property
    def mean_outdoor(self) -> float | None:
        # Only when the outdoor temperature was known for most of the stretch
        if self.n_outdoor == 0 or self.n_outdoor < self.n / 2:
            return None
        return self.sum_outdoor / self.n_outdoor


@dataclass
class _Stretch:
    """The stretch in progress for one room."""

    heated: bool
    start: datetime
    start_temperature: float
    after_heat: bool                  # not heated, straight after heating: let radiators cool first
    follows_heat: bool = False        # after_heat, and the heated stretch just before is known
    continued: bool = False           # carries on a longer stretch: no warm-up to learn
    last: datetime | None = None
    last_temperature: float | None = None
    rise_at: datetime | None = None   # heated: when the room first showed a rise (live estimates)
    fit: _LineFit | None = None
    # Heated: every reading (hours since start, °C), fitted as warm-up + climb at the end
    readings: list[tuple[float, float]] = field(default_factory=list)
    # Heated: what the stretch started from, and running means, for the log
    context: dict[str, Any] = field(default_factory=dict)
    sum_outdoor: float = 0.0
    n_outdoor: int = 0
    sum_trvs: float = 0.0
    n_trvs: int = 0
    # Not heated, after heat: the overrun until it settles (then cooling is fitted)
    settled: bool = False
    peak: float | None = None
    peak_first: datetime | None = None  # first reading at the peak
    peak_last: datetime | None = None   # latest reading at the peak
    overrun_readings: list[tuple[datetime, float, float | None]] = field(default_factory=list)

    @property
    def in_overrun(self) -> bool:
        return not self.heated and self.after_heat and not self.settled


@dataclass
class _Room:
    """What's learned for one room."""

    heating_rate: float | None = None
    warm_up_minutes: float | None = None
    heating_stretches: int = 0
    cooling_rate: float | None = None
    cooling_stretches: int = 0
    heat_loss: float | None = None    # °C/h lost per °C warmer than outside
    heat_loss_stretches: int = 0
    heating_power: float | None = None    # °C/h the radiators add before losses to outside
    heating_power_stretches: int = 0
    overrun_rise: float | None = None     # °C the room keeps rising after heating stops
    overrun_minutes: float | None = None  # from heating stopping to the peak
    overrun_stretches: int = 0
    last_heated: bool | None = None   # state of the previous stretch
    last_heated_at: datetime | None = None  # end of the last heated stretch
    log: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # The last heated stretch's log entry, until the overrun after it is known
    awaiting_overrun: dict[str, Any] | None = field(default=None, repr=False)
    stretch: _Stretch | None = field(default=None, repr=False)

    LEARNED = (
        "heating_rate", "warm_up_minutes", "heating_stretches",
        "cooling_rate", "cooling_stretches", "heat_loss", "heat_loss_stretches",
        "overrun_rise", "overrun_minutes", "overrun_stretches",
        "heating_power", "heating_power_stretches",
    )

    def to_dict(self) -> dict[str, Any]:
        data = {key: getattr(self, key) for key in self.LEARNED}
        data["last_heated_at"] = None if self.last_heated_at is None else self.last_heated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _Room:
        room = cls()
        for key in cls.LEARNED:
            value = data.get(key)
            if value is None:
                continue
            setattr(room, key, int(value) if key.endswith("stretches") else float(value))
        if (last_heated_at := data.get("last_heated_at")) is not None:
            room.last_heated_at = dt_util.parse_datetime(last_heated_at)
        return room


class HeatingAnalytics:
    """Learns each room's warm-up time, heating rate, cooling rate and heat loss."""

    def __init__(self, min_stretches: int, learning_rate: float):
        """
        Args:
            min_stretches: stretches a room needs before a rate (and estimates using it) is shown
            learning_rate: how much each new stretch moves a learned figure (0-1)
        """
        self.min_stretches = max(1, int(min_stretches))
        self.learning_rate = learning_rate
        self.rooms: dict[str, dict[str, _Room]] = {}
        # Goes up whenever a stretch is logged, so the log is saved only when it changed
        self.log_version = 0

    def _room(self, zone_id: str, room_id: str) -> _Room:
        return self.rooms.setdefault(zone_id, {}).setdefault(room_id, _Room())

    def _learn(self, old: float | None, new: float) -> float:
        return new if old is None else self.learning_rate * new + (1 - self.learning_rate) * old

    # -- recording ------------------------------------------------------------

    def record_temperature(
        self,
        zone_id: str,
        room_id: str,
        temp: float,
        needs_heating: bool,
        timestamp: datetime | None = None,
        heat_flowing: bool | None = None,
        own_sensors: bool = True,
        outdoor_temp: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Add a reading.

        Args:
            temp: room temperature (°C)
            needs_heating: the room wants heat
            heat_flowing: the zone's heat source is on (None: not known, assume it is)
            own_sensors: temp comes from the room's own sensors; other readings
                aren't learned from and end the stretch
            outdoor_temp: outdoor temperature (°C), if known
            context: for the log of heated stretches: target, trvs_heating (TRVs
                across the house taking heat), zone_heat_minutes and
                house_heat_minutes (how long heat has been flowing in the zone and
                anywhere; None if not known)
        """
        timestamp = dt_util.utcnow() if timestamp is None else dt_util.as_utc(timestamp)
        heated = bool(needs_heating) and heat_flowing is not False
        room = self._room(zone_id, room_id)
        stretch = room.stretch

        if not own_sensors:
            self._end_stretch(room, learn=False)
            return
        if stretch is not None and stretch.last is not None:
            if timestamp <= stretch.last:
                return
            if timestamp - stretch.last > MAX_SAMPLE_GAP:
                self._end_stretch(room, learn=False)
                stretch = None
        if stretch is not None and stretch.heated != heated:
            self._end_stretch(room, learn=True)
            stretch = None
        if stretch is not None and stretch.last is not None and stretch.last - stretch.start >= MAX_STRETCH_SPAN:
            # Learn from what's there and carry on without a gap
            self._end_stretch(room, learn=True)
            stretch = self._start(room, heated, timestamp, temp, continued=True, context=context)
        if stretch is None:
            stretch = self._start(room, heated, timestamp, temp, context=context)
        self._add(room, stretch, timestamp, temp, outdoor_temp, context)

    def _start(
        self, room: _Room, heated: bool, timestamp: datetime, temp: float,
        continued: bool = False, context: dict[str, Any] | None = None,
    ) -> _Stretch:
        # After a restart the previous state isn't known: assume radiators may still be warm
        after_heat = not heated and room.last_heated is not False and not continued
        stretch = _Stretch(
            heated=heated, start=timestamp, start_temperature=temp,
            after_heat=after_heat, continued=continued,
            # The overrun is only learned straight after a heated stretch that was seen to end
            follows_heat=after_heat and room.last_heated is True and room.last_heated_at is not None
            and timestamp - room.last_heated_at <= MAX_SAMPLE_GAP,
        )
        if not stretch.follows_heat:
            room.awaiting_overrun = None
        if heated:
            since = None if room.last_heated_at is None else _minutes(timestamp - room.last_heated_at)
            stretch.context = {**(context or {}), "minutes_since_heated": 0 if continued else since}
        if continued and heated:
            stretch.rise_at = timestamp           # already warming
        room.stretch = stretch
        return stretch

    def _add(
        self, room: _Room, stretch: _Stretch, timestamp: datetime, temp: float,
        outdoor: float | None, context: dict[str, Any] | None = None,
    ) -> None:
        if stretch.heated:
            if outdoor is not None:
                stretch.sum_outdoor += outdoor
                stretch.n_outdoor += 1
            if context and context.get("trvs_heating") is not None:
                stretch.sum_trvs += context["trvs_heating"]
                stretch.n_trvs += 1
            stretch.readings.append(((timestamp - stretch.start).total_seconds() / 3600.0, temp))
            if stretch.rise_at is None and temp >= stretch.start_temperature + RISE_THRESHOLD - 1e-9:
                stretch.rise_at = timestamp
                stretch.fit = _LineFit(origin=stretch.start)
                # The warming starts from the last reading before the rise
                if stretch.last is not None and stretch.last_temperature is not None:
                    stretch.fit.add(stretch.last, stretch.last_temperature, outdoor)
            if stretch.fit is not None:
                stretch.fit.add(timestamp, temp, outdoor)
        elif stretch.in_overrun:
            self._add_overrun(room, stretch, timestamp, temp, outdoor)
        else:
            if stretch.fit is None:
                stretch.fit = _LineFit(origin=stretch.start)
            stretch.fit.add(timestamp, temp, outdoor)
        stretch.last = timestamp
        stretch.last_temperature = temp

    def _add_overrun(
        self, room: _Room, stretch: _Stretch, timestamp: datetime, temp: float, outdoor: float | None,
    ) -> None:
        """A reading while the room may still be warming from radiators after heating."""
        stretch.overrun_readings.append((timestamp, temp, outdoor))
        if stretch.peak is None or temp > stretch.peak + 1e-9:
            stretch.peak, stretch.peak_first, stretch.peak_last = temp, timestamp, timestamp
        elif temp >= stretch.peak - 1e-9:
            stretch.peak_last = timestamp
        elapsed = timestamp - stretch.start
        if elapsed < COOL_SETTLE:
            return
        if not (
            temp <= stretch.peak - RISE_THRESHOLD + 1e-9
            or timestamp - stretch.peak_first >= OVERRUN_FLAT
            or elapsed >= MAX_OVERRUN
        ):
            return
        # Over: cooling is fitted from the last reading at the peak on
        stretch.settled = True
        stretch.fit = _LineFit(origin=stretch.start)
        for when, value, out in stretch.overrun_readings:
            if when >= stretch.peak_last:
                stretch.fit.add(when, value, out)
        stretch.overrun_readings = []
        if not stretch.follows_heat:
            return
        rise = max(0.0, stretch.peak - stretch.start_temperature)
        minutes = (stretch.peak_first - stretch.start).total_seconds() / 60
        room.overrun_rise = self._learn(room.overrun_rise, rise)
        room.overrun_minutes = self._learn(room.overrun_minutes, minutes)
        room.overrun_stretches += 1
        if room.awaiting_overrun is not None:
            room.awaiting_overrun["overrun_rise"] = _round(rise)
            room.awaiting_overrun["overrun_minutes"] = _round(minutes, 1)
            room.awaiting_overrun = None
            self.log_version += 1

    def _end_stretch(self, room: _Room, learn: bool) -> None:
        stretch = room.stretch
        room.stretch = None
        if stretch is None:
            return
        room.last_heated = stretch.heated
        if stretch.heated and stretch.last is not None:
            room.last_heated_at = stretch.last
        if not learn or stretch.last is None:
            return
        fit = stretch.fit
        if stretch.heated:
            result = _warm_up_and_climb(stretch.readings)
            warm_up = None
            if result is not None:
                warm_up_hours, slope = result
                if not stretch.continued and stretch.last - stretch.start >= MIN_HEATED_STRETCH:
                    warm_up = warm_up_hours * 60
                    room.warm_up_minutes = self._learn(room.warm_up_minutes, warm_up)
                room.heating_rate = self._learn(room.heating_rate, slope)
                room.heating_stretches += 1
                power = self._heating_power(room, stretch, slope)
                if power is not None:
                    room.heating_power = self._learn(room.heating_power, power)
                    room.heating_power_stretches += 1
            if len(stretch.readings) >= 2:
                entry = _log_entry(stretch, warm_up, None if result is None else result[1])
                room.log.append(entry)
                del room.log[:-LOG_SIZE]
                room.awaiting_overrun = entry
                self.log_version += 1
            return
        if fit is None or fit.span < MIN_COOLING_SPAN or (slope := fit.slope()) is None:
            return
        room.cooling_rate = self._learn(room.cooling_rate, slope)
        room.cooling_stretches += 1
        outdoor = fit.mean_outdoor
        if outdoor is not None:
            difference = fit.mean_temperature - outdoor
            if difference >= MIN_INDOOR_OUTDOOR_DIFFERENCE and slope < 0:
                room.heat_loss = self._learn(room.heat_loss, -slope / difference)
                room.heat_loss_stretches += 1

    def _heating_power(self, room: _Room, stretch: _Stretch, rate: float) -> float | None:
        """The radiators' own heating rate in this stretch: the room's rate plus what it
        lost to outside meanwhile. None without heat loss or the outdoor temperature."""
        if room.heat_loss is None or room.heat_loss_stretches < self.min_stretches:
            return None
        if stretch.n_outdoor == 0 or stretch.n_outdoor < len(stretch.readings) / 2:
            return None
        room_mean = sum(temp for _, temp in stretch.readings) / len(stretch.readings)
        outdoor = stretch.sum_outdoor / stretch.n_outdoor
        return rate + room.heat_loss * max(0.0, room_mean - outdoor)

    def _expected_heating_rate(
        self, room: _Room, current: float, target: float, outdoor: float | None,
    ) -> float | None:
        """Heating rate to expect today: heating power less the losses at today's outdoor
        temperature, over the climb from current to target. None if not known."""
        enough = self.min_stretches
        if (
            outdoor is None or room.heating_power is None or room.heating_power_stretches < enough
            or room.heat_loss is None or room.heat_loss_stretches < enough
        ):
            return None
        middle = (current + target) / 2
        return room.heating_power - room.heat_loss * max(0.0, middle - outdoor)

    def get_log(self, zone_id: str, room_id: str) -> list[dict[str, Any]]:
        """The room's logged heated stretches, oldest first."""
        room = self.rooms.get(zone_id, {}).get(room_id)
        return [] if room is None else list(room.log)

    def skip_reading(self, zone_id: str, room_id: str) -> None:
        """No usable temperature this update: end the stretch without learning from it."""
        self._end_stretch(self._room(zone_id, room_id), learn=False)

    # -- results ----------------------------------------------------------------

    def get_analytics(
        self,
        zone_id: str,
        room_id: str,
        current_temp: float,
        target_temp: float,
        needs_heating: bool,
        outdoor_temp: float | None = None,
        now: datetime | None = None,
        target_changes_at: datetime | None = None,
    ) -> HeatingAnalyticsData:
        """What's learned for a room, the stretch in progress, and time to reach target.

        target_changes_at: when the room's target next changes (schedule, end of a
        boost); an estimate beyond it isn't given.
        """
        now = dt_util.utcnow() if now is None else dt_util.as_utc(now)
        room = self._room(zone_id, room_id)
        enough = self.min_stretches
        heating_rate = room.heating_rate if room.heating_stretches >= enough else None
        cooling_rate = room.cooling_rate if room.cooling_stretches >= enough else None
        heat_loss = room.heat_loss if room.heat_loss_stretches >= enough else None
        warm_up = room.warm_up_minutes if room.heating_stretches >= enough else None
        overrun_known = room.overrun_stretches >= enough

        stretch = room.stretch
        state = None if stretch is None else ("heated" if stretch.heated else "not_heated")
        minutes = None if stretch is None or stretch.last is None else int((stretch.last - stretch.start).total_seconds() // 60)
        rate_so_far = None
        if stretch is not None and stretch.fit is not None and stretch.fit.n >= 3 and stretch.fit.span >= MIN_LIVE_SPAN:
            rate_so_far = stretch.fit.slope()
        elif stretch is not None and stretch.in_overrun:
            rate_so_far = _recent_slope(stretch.overrun_readings)

        trend_rate = rate_so_far
        if trend_rate is None and stretch is not None:
            trend_rate = heating_rate if stretch.heated else cooling_rate

        # Warming: at today's outdoor temperature when the heating power is known
        expected_rate = self._expected_heating_rate(room, current_temp, target_temp, outdoor_temp)
        eta_minutes, confidence, eta_status = self._time_to_target(
            room, stretch, current_temp, target_temp, needs_heating, outdoor_temp,
            heating_rate if expected_rate is None else expected_rate,
            warm_up, cooling_rate, heat_loss, rate_so_far,
            heating_stretches=room.heating_stretches if expected_rate is None else room.heating_power_stretches,
        )
        if (
            expected_rate is not None and expected_rate < STABLE_RATE
            and needs_heating and current_temp < target_temp
        ):
            # Losses at today's outdoor temperature outweigh what the radiators add
            eta_status = "not_reaching"
        if (
            eta_minutes is not None and target_changes_at is not None
            and now + timedelta(minutes=eta_minutes) > dt_util.as_utc(target_changes_at)
        ):
            # Not before the target changes: an estimate for this target means nothing
            eta_minutes, confidence, eta_status = None, 0.0, "not_before_target_changes"
        eta_timestamp = (
            dt_util.as_local(now + timedelta(minutes=eta_minutes)) if eta_minutes is not None else None
        )

        # While still warming after heating: the peak to expect
        expected_peak = expected_peak_minutes = None
        if stretch is not None and stretch.in_overrun and overrun_known and stretch.peak is not None:
            expected_peak = max(stretch.peak, stretch.start_temperature + room.overrun_rise)
            elapsed = (now - stretch.start).total_seconds() / 60
            expected_peak_minutes = max(0, round(room.overrun_minutes - elapsed))
        return HeatingAnalyticsData(
            heating_rate=heating_rate,
            warm_up_minutes=None if warm_up is None else round(warm_up),
            heating_stretches=room.heating_stretches,
            cooling_rate=cooling_rate,
            heat_loss_per_degree=heat_loss,
            cooling_stretches=room.cooling_stretches,
            current_state=state,
            current_minutes=minutes,
            rate_so_far=rate_so_far,
            trend=_trend(trend_rate),
            eta_minutes=eta_minutes,
            eta_timestamp=eta_timestamp,
            confidence=confidence,
            logged_stretches=len(room.log),
            heating_power=room.heating_power if room.heating_power_stretches >= enough else None,
            expected_heating_rate=expected_rate,
            overrun_rise=room.overrun_rise if overrun_known else None,
            overrun_minutes=None if not overrun_known else round(room.overrun_minutes),
            overrun_stretches=room.overrun_stretches,
            current_overrun=None if stretch is None else stretch.in_overrun,
            eta_status=eta_status,
            expected_peak=expected_peak,
            expected_peak_minutes=expected_peak_minutes,
        )

    def _time_to_target(
        self, room: _Room, stretch: _Stretch | None, current: float, target: float,
        needs_heating: bool, outdoor: float | None,
        heating_rate: float | None, warm_up: float | None,
        cooling_rate: float | None, heat_loss: float | None,
        rate_so_far: float | None = None,
        heating_stretches: int | None = None,
    ) -> tuple[int | None, float, str | None]:
        """(minutes, confidence, status) to reach target: warming if the room wants heat,
        else cooling. Status says why there's no estimate: still_warming (radiators still
        giving heat after heating), not_cooling (warming from something else)."""
        if needs_heating and current < target:
            if heating_rate is None or heating_rate < 0.05:
                return None, 0.0, None
            # Warm-up still to come: all of it unless this heated stretch has started warming
            remaining_warm_up = warm_up or 0.0
            if stretch is not None and stretch.heated:
                if stretch.rise_at is not None:
                    remaining_warm_up = 0.0
                elif stretch.last is not None:
                    elapsed = (stretch.last - stretch.start).total_seconds() / 60
                    remaining_warm_up = max(0.0, remaining_warm_up - elapsed)
            hours = (target - current) / heating_rate
            stretches = room.heating_stretches if heating_stretches is None else heating_stretches
            return round(remaining_warm_up + hours * 60), self._confidence(stretches, hours), "warming"
        if not needs_heating and current > target:
            if stretch is not None and stretch.in_overrun:
                return None, 0.0, "still_warming"
            if rate_so_far is not None and rate_so_far > STABLE_RATE:
                return None, 0.0, "not_cooling"
            if heat_loss is not None and outdoor is not None and target - outdoor > 0.5:
                # Cooling slows as the room nears the outside temperature
                hours = math.log((current - outdoor) / (target - outdoor)) / heat_loss
                return round(hours * 60), self._confidence(room.heat_loss_stretches, hours), "cooling"
            if cooling_rate is not None and cooling_rate < -0.05:
                hours = (current - target) / -cooling_rate
                return round(hours * 60), self._confidence(room.cooling_stretches, hours), "cooling"
        return None, 0.0, None

    def _confidence(self, stretches: int, hours: float) -> float:
        confidence = min(0.9, 0.5 + 0.1 * (stretches - self.min_stretches))
        if hours > 2:
            confidence *= 0.7
        return round(confidence, 2)

    # -- storage ----------------------------------------------------------------

    def get_history_for_storage(self) -> dict:
        """Learned figures for every room (not the stretch in progress)."""
        return {
            "format": STORAGE_FORMAT,
            "rooms": {
                zone_id: {room_id: room.to_dict() for room_id, room in rooms.items()}
                for zone_id, rooms in self.rooms.items()
            },
        }

    def restore_history(self, stored: dict) -> None:
        """Restore learned figures. Data from before 3.6 (reading histories) is dropped:
        its rates came from a method that read coarse sensors as not warming."""
        if not isinstance(stored, dict) or stored.get("format") != STORAGE_FORMAT:
            if stored:
                _LOGGER.info("Heating analytics start afresh: older data isn't compatible")
            return
        for zone_id, rooms in (stored.get("rooms") or {}).items():
            for room_id, data in (rooms or {}).items():
                try:
                    self.rooms.setdefault(zone_id, {})[room_id] = _Room.from_dict(data)
                except (TypeError, ValueError, AttributeError) as err:
                    _LOGGER.warning("Could not restore analytics for %s/%s: %s", zone_id, room_id, err)

    def get_log_for_storage(self) -> dict:
        """Every room's log, one comma-separated line per stretch (columns named once)."""
        return {
            "format": LOG_FORMAT,
            "columns": list(LOG_COLUMNS),
            "rooms": {
                zone_id: {room_id: [_encode(entry) for entry in room.log] for room_id, room in rooms.items() if room.log}
                for zone_id, rooms in self.rooms.items()
                if any(room.log for room in rooms.values())
            },
        }

    def restore_log(self, stored: Any, keep: dict[str, Any] | None = None) -> None:
        """Restore the log. Rooms not in `keep` (zone_id -> room ids), if given, are dropped;
        unreadable lines are skipped. Columns are matched by name, so a log saved with
        more or fewer columns still loads."""
        if not isinstance(stored, dict) or stored.get("format") != LOG_FORMAT:
            return
        columns = stored.get("columns")
        if not isinstance(columns, list):
            return
        for zone_id, rooms in (stored.get("rooms") or {}).items():
            if not isinstance(rooms, dict):
                continue
            for room_id, lines in rooms.items():
                if keep is not None and room_id not in keep.get(zone_id, ()):
                    continue
                if not isinstance(lines, list):
                    continue
                log = [entry for line in lines if (entry := _decode(columns, line)) is not None]
                if log:
                    self._room(zone_id, room_id).log = log[-LOG_SIZE:]


def _encode(entry: dict[str, Any]) -> str:
    values = []
    for column in LOG_COLUMNS:
        value = entry.get(column)
        if value is None:
            values.append("")
        elif column in _LOG_BOOL:
            values.append("1" if value else "0")
        elif isinstance(value, float) and value.is_integer():
            values.append(str(int(value)))
        else:
            values.append(str(value))
    return ",".join(values)


def _decode(columns: list[str], line: Any) -> dict[str, Any] | None:
    if not isinstance(line, str):
        return None
    values = line.split(",")
    if len(values) != len(columns):
        return None
    entry: dict[str, Any] = dict.fromkeys(LOG_COLUMNS)
    try:
        for column, value in zip(columns, values):
            if column not in entry or value == "":
                continue
            if column in _LOG_TEXT:
                entry[column] = value
            elif column in _LOG_BOOL:
                entry[column] = value == "1"
            elif column in _LOG_INT:
                entry[column] = int(value)
            else:
                entry[column] = float(value)
    except ValueError:
        return None
    if entry["start"] is None or dt_util.parse_datetime(entry["start"]) is None:
        return None
    return entry


def _recent_slope(readings: list[tuple[datetime, float, float | None]]) -> float | None:
    """°C/h over the last OVERRUN_RATE_WINDOW of readings (None if too few)."""
    if not readings:
        return None
    since = readings[-1][0] - OVERRUN_RATE_WINDOW
    fit = _LineFit(origin=readings[0][0])
    for when, value, _ in readings:
        if when >= since:
            fit.add(when, value, None)
    if fit.n < 3 or fit.span < MIN_LIVE_SPAN:
        return None
    return fit.slope()


def _minutes(delta: timedelta) -> float:
    return round(delta.total_seconds() / 60, 1)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _log_entry(stretch: _Stretch, warm_up: float | None, rate: float | None) -> dict[str, Any]:
    """One heated stretch for the log: what it started from and what it showed."""
    context = stretch.context
    target = context.get("target")
    return {
        "start": stretch.start.isoformat(),
        "minutes": _minutes(stretch.last - stretch.start),
        # Carries on a longer stretch: already warming, no warm-up
        "continued": stretch.continued,
        "start_temp": _round(stretch.start_temperature),
        "end_temp": _round(stretch.last_temperature),
        "target": _round(target),
        "climb_needed": None if target is None else _round(target - stretch.start_temperature),
        "outdoor": _round(stretch.sum_outdoor / stretch.n_outdoor, 1) if stretch.n_outdoor else None,
        "warm_up_minutes": _round(warm_up, 1),
        "rate": _round(rate, 3),
        # How hot the system already was: minutes heat had flowed in the zone / anywhere (0: cold start)
        "zone_heat_minutes": context.get("zone_heat_minutes"),
        "house_heat_minutes": context.get("house_heat_minutes"),
        # How many radiators shared the heat: TRVs in heated rooms across the house
        "trvs_heating": context.get("trvs_heating"),
        "trvs_heating_mean": _round(stretch.sum_trvs / stretch.n_trvs, 1) if stretch.n_trvs else None,
        # Radiator still warm? Minutes since this room's last heated stretch ended
        "minutes_since_heated": context.get("minutes_since_heated"),
        # Filled in once the overrun after this stretch is known
        "overrun_rise": None,
        "overrun_minutes": None,
    }


def _warm_up_and_climb(readings: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Fit a heated stretch as flat (radiators warming) bending into a straight climb.

    The model is temperature = level + climb × max(0, time - bend). Every reading
    time is tried as the bend and the best least-squares fit kept; running sums
    keep it quick. Noise and coarse sensor steps barely move the bend, unlike
    spotting the first rise. Returns (warm-up hours, climb in °C/h), or None if
    there's no climb of at least RISE_THRESHOLD lasting at least MIN_RISE_SPAN.
    """
    n = len(readings)
    if n < 3:
        return None
    min_span = MIN_RISE_SPAN.total_seconds() / 3600.0
    total_y = sum(y for _, y in readings)
    total_yy = sum(y * y for _, y in readings)
    # Sums over readings k..n-1: count, t, t², y, t·y
    suffix = [(0, 0.0, 0.0, 0.0, 0.0)] * (n + 1)
    for i in range(n - 1, -1, -1):
        t, y = readings[i]
        c, st, stt, sy, sty = suffix[i + 1]
        suffix[i] = (c + 1, st + t, stt + t * t, sy + y, sty + t * y)
    best = None
    end = readings[-1][0]
    for k in range(n):
        bend = readings[k][0]
        if end - bend < min_span:
            break
        c, st, stt, sy, sty = suffix[k]
        h = st - c * bend                              # Σ (t - bend) over the climb
        hh = stt - 2 * bend * st + c * bend * bend     # Σ (t - bend)²
        hy = sty - bend * sy                           # Σ y (t - bend)
        det = n * hh - h * h
        if det <= 1e-12:
            continue
        level = (total_y * hh - h * hy) / det
        climb = (n * hy - h * total_y) / det
        error = total_yy - level * total_y - climb * hy
        if best is None or error < best[0] - 1e-12:
            best = (error, bend, climb)
    if best is None:
        return None
    _, bend, climb = best
    # A real climb: the room warmed by at least one sensor step, not just noise
    if climb * (end - bend) < RISE_THRESHOLD:
        return None
    return bend, climb


def _trend(rate: float | None) -> str:
    if rate is None:
        return "insufficient_data"
    if rate > 1.0:
        return "heating_rapidly"
    if rate > 0.2:
        return "heating_slowly"
    if rate >= -0.2:
        return "stable"
    if rate >= -1.0:
        return "cooling_slowly"
    return "cooling_rapidly"
