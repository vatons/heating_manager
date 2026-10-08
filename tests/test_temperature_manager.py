"""Tests for TemperatureManager (sensor reading, freshness, fallbacks)."""
from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant

from custom_components.heating_manager.temperature_manager import TemperatureManager

from .conftest import local_dt, set_last_seen, set_temp

NOW = local_dt(hour=12)


def zones_with(rooms: dict) -> dict:
    return {"z": {"rooms": rooms}}


@pytest.fixture
def tm(hass):
    return TemperatureManager(hass)


async def read(tm, room_cfg, zones=None, room_id="r"):
    zones = zones or zones_with({room_id: room_cfg})
    return await tm.get_room_temperature("z", room_id, room_cfg, zones)


@pytest.fixture(autouse=True)
def _frozen(freezer):
    freezer.move_to(NOW)
    return freezer


async def test_single_string_sensor(hass, tm):
    set_temp(hass, "sensor.t", 19.5)
    temp, meta = await read(tm, {"sensors": ["sensor.t"]})
    assert temp == 19.5
    assert meta["source"] == "local_sensors"
    assert meta["sensors_status"][0]["status"] == "active"
    assert meta["sensors_status"][0]["last_seen_source"] == "state_last_reported"


async def test_multiple_sensors_are_averaged(hass, tm):
    set_temp(hass, "sensor.a", 19.0)
    set_temp(hass, "sensor.b", 20.0)
    set_temp(hass, "sensor.c", 21.0)
    temp, _ = await read(
        tm, {"sensors": ["sensor.a", {"temperature": "sensor.b"}, {"temperature": "sensor.c"}]}
    )
    assert temp == pytest.approx(20.0)


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "not-a-number"])
async def test_bad_sensor_is_skipped(hass, tm, bad):
    set_temp(hass, "sensor.a", 19.0)
    hass.states.async_set("sensor.b", bad)
    temp, meta = await read(tm, {"sensors": ["sensor.a", "sensor.b"]})
    assert temp == 19.0
    statuses = {s["entity_id"]: s["status"] for s in meta["sensors_status"]}
    assert statuses["sensor.b"] in ("unavailable", "invalid")


async def test_missing_entity_is_skipped(hass, tm):
    set_temp(hass, "sensor.a", 19.0)
    temp, _ = await read(tm, {"sensors": ["sensor.a", "sensor.missing"]})
    assert temp == 19.0


async def test_out_of_range_reading_rejected(hass, tm):
    set_temp(hass, "sensor.a", 19.0)
    set_temp(hass, "sensor.b", 85.0)
    temp, meta = await read(tm, {"sensors": ["sensor.a", "sensor.b"]})
    assert temp == 19.0
    assert meta["sensors_status"][1]["status"] == "invalid"


async def test_implausible_jump_rejected(hass, tm, _frozen):
    cfg = {"sensors": ["sensor.a"]}
    set_temp(hass, "sensor.a", 19.0)
    assert (await read(tm, cfg))[0] == 19.0
    _frozen.tick(timedelta(minutes=1))
    set_temp(hass, "sensor.a", 25.0)       # +6°C in a minute
    temp, meta = await read(tm, cfg)
    assert meta["sensors_status"][0]["status"] == "invalid"
    assert temp == 19.0                     # last known value used


async def test_dedicated_last_seen_sensor_used(hass, tm, _frozen):
    set_temp(hass, "sensor.t", 19.0)
    set_last_seen(hass, "sensor.t_last_seen", NOW - timedelta(minutes=5))
    temp, meta = await read(tm, {"sensors": [{"temperature": "sensor.t", "last_seen": "sensor.t_last_seen"}]})
    assert temp == 19.0
    assert meta["sensors_status"][0]["last_seen_source"] == "dedicated_sensor"


async def test_stale_last_seen_times_out(hass, tm):
    set_temp(hass, "sensor.t", 19.0)
    set_last_seen(hass, "sensor.t_last_seen", NOW - timedelta(minutes=45))
    set_temp(hass, "sensor.other", 21.0)
    zones = zones_with({
        "r": {"sensors": [{"temperature": "sensor.t", "last_seen": "sensor.t_last_seen"}]},
        "other": {"sensors": ["sensor.other"]},
    })
    temp, meta = await read(tm, zones["z"]["rooms"]["r"], zones)
    assert meta["sensors_status"][0]["status"] == "timeout"
    assert meta["source"] == "zone_average"


async def test_unparseable_last_seen_falls_back_to_last_updated(hass, tm):
    set_temp(hass, "sensor.t", 19.0)
    hass.states.async_set("sensor.t_last_seen", "1700000000000")    # epoch ms format
    temp, meta = await read(tm, {"sensors": [{"temperature": "sensor.t", "last_seen": "sensor.t_last_seen"}]})
    assert temp == 19.0
    assert meta["sensors_status"][0]["last_seen_source"] == "state_last_reported"


async def test_naive_last_seen_is_accepted(hass, tm):
    set_temp(hass, "sensor.t", 19.0)
    hass.states.async_set("sensor.t_last_seen", (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat())
    temp, meta = await read(tm, {"sensors": [{"temperature": "sensor.t", "last_seen": "sensor.t_last_seen"}]})
    assert temp == 19.0
    assert meta["sensors_status"][0]["status"] == "active"


async def test_no_sensors_uses_zone_average(hass, tm):
    set_temp(hass, "sensor.a", 18.0)
    set_temp(hass, "sensor.b", 20.0)
    zones = zones_with({
        "r": {"name": "no sensors"},
        "a": {"sensors": ["sensor.a"]},
        "b": {"sensors": [{"temperature": "sensor.b"}]},
    })
    temp, meta = await read(tm, zones["z"]["rooms"]["r"], zones)
    assert temp == pytest.approx(19.0)
    assert meta["source"] == "zone_average"


async def test_zone_average_excludes_stale_sensors(hass, tm, _frozen):
    set_temp(hass, "sensor.a", 10.0)
    _frozen.tick(timedelta(minutes=45))
    set_temp(hass, "sensor.b", 20.0)
    zones = zones_with({"r": {}, "a": {"sensors": ["sensor.a"]}, "b": {"sensors": ["sensor.b"]}})
    temp, _ = await read(tm, zones["z"]["rooms"]["r"], zones)
    assert temp == 20.0


async def test_everything_stale_returns_none(hass, tm, _frozen):
    set_temp(hass, "sensor.a", 19.0)
    _frozen.tick(timedelta(minutes=45))
    temp, meta = await read(tm, {"sensors": ["sensor.a"]})
    assert temp is None


async def test_last_known_value_used_when_sensor_goes_unavailable(hass, tm, _frozen):
    cfg = {"sensors": ["sensor.a"]}
    set_temp(hass, "sensor.a", 19.0)
    await read(tm, cfg)
    _frozen.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.a", "unavailable")
    temp, meta = await read(tm, cfg)
    assert temp == 19.0
    assert meta["source"] == "local_sensors"


async def test_get_sensor_entity_ids(tm):
    cfg = {"sensors": ["sensor.a", {"temperature": "sensor.b", "last_seen": "sensor.x"}, {"last_seen": "x"}, 5]}
    assert tm.get_sensor_entity_ids(cfg) == ["sensor.a", "sensor.b"]


async def test_steady_sensor_still_reporting_is_not_stale(hass: HomeAssistant, tm, _frozen):
    cfg = {"sensors": ["sensor.hallway_temperature"]}
    set_temp(hass, "sensor.hallway_temperature", 19.0)
    for _ in range(8):                     # reports the same value every 5 min
        _frozen.tick(timedelta(minutes=5))
        set_temp(hass, "sensor.hallway_temperature", 19.0)
    temp, meta = await read(tm, cfg)
    assert meta["sensors_status"][0]["status"] == "active"
    assert temp == 19.0


async def test_zone_average_includes_steady_sensors(hass, tm, _frozen):
    set_temp(hass, "sensor.a", 18.0)
    for _ in range(8):
        _frozen.tick(timedelta(minutes=5))
        set_temp(hass, "sensor.a", 18.0)
    zones = zones_with({"r": {}, "a": {"sensors": ["sensor.a"]}})
    temp, _ = await read(tm, zones["z"]["rooms"]["r"], zones)
    assert temp == 18.0


async def test_sensor_that_stops_reporting_still_times_out(hass, tm, _frozen):
    """last_reported must not mask a genuinely dead sensor."""
    cfg = {"sensors": ["sensor.a"]}
    set_temp(hass, "sensor.a", 19.0)
    _frozen.tick(timedelta(minutes=31))
    temp, meta = await read(tm, cfg)
    assert meta["sensors_status"][0]["status"] == "timeout"
    assert temp is None


async def test_last_seen_with_utc_offset_still_supported(hass, tm):
    set_temp(hass, "sensor.t", 19.0)
    hass.states.async_set("sensor.t_last_seen", "2026-01-14T20:00:00+00:00")   # == NOW (12:00 -08:00)
    temp, meta = await read(tm, {"sensors": [{"temperature": "sensor.t", "last_seen": "sensor.t_last_seen"}]})
    assert meta["sensors_status"][0]["status"] == "active"
