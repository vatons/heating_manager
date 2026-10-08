"""Tests for BoostManager."""
from datetime import timedelta

import pytest

from custom_components.heating_manager.boost_manager import BoostManager
from custom_components.heating_manager.const import MAX_BOOST_TEMP

from .conftest import local_dt

NOW = local_dt(hour=12)
CONFIG = {
    "zones": {
        "z": {
            "rooms": {
                "r": {"sensors": ["sensor.t"], "trvs": ["climate.t"]},
                "nosensor": {"trvs": ["climate.n"]},
            }
        }
    }
}


def temp_cb(value):
    async def _cb(zone_id, room_id, room_cfg, zones):
        return value, {}
    return _cb


@pytest.fixture
def bm(hass, freezer):
    freezer.move_to(NOW)
    return BoostManager(hass, boost_duration=30)


async def test_set_boost_with_explicit_temperature(bm):
    assert await bm.set_boost("z", "r", CONFIG, duration=60, temperature=22.0)
    info = bm.get_boost_info("z", "r", NOW)
    assert info["temperature"] == 22.0
    assert info["duration"] == 60
    assert info["end_time"] == NOW + timedelta(minutes=60)


async def test_default_duration(bm):
    await bm.set_boost("z", "r", CONFIG, temperature=22.0)
    assert bm.get_boost_info("z", "r", NOW)["duration"] == 30


async def test_default_temperature_from_room_temp(bm):
    assert await bm.set_boost("z", "r", CONFIG, get_room_temp_callback=temp_cb(18.5))
    assert bm.get_boost_info("z", "r", NOW)["temperature"] == pytest.approx(20.5)


@pytest.mark.parametrize(
    ("zone", "room", "kwargs"),
    [
        ("missing", "r", {"temperature": 22.0}),
        ("z", "missing", {"temperature": 22.0}),
        ("z", "nosensor", {"temperature": 22.0}),
        ("z", "r", {}),                                         # no callback
        ("z", "r", {"get_room_temp_callback": temp_cb(None)}),  # temp unavailable
    ],
)
async def test_set_boost_rejections(bm, zone, room, kwargs):
    assert await bm.set_boost(zone, room, CONFIG, **kwargs) is False
    assert bm.boost_state == {}


async def test_boost_clamped_to_max(bm):
    await bm.set_boost("z", "r", CONFIG, temperature=45.0)
    assert bm.get_boost_info("z", "r", NOW)["temperature"] == MAX_BOOST_TEMP


async def test_boost_expires(bm):
    await bm.set_boost("z", "r", CONFIG, duration=30, temperature=22.0)
    assert bm.get_boost_info("z", "r", NOW + timedelta(minutes=29))
    assert bm.get_boost_info("z", "r", NOW + timedelta(minutes=31)) is None
    assert bm.boost_state == {}


async def test_update_temperature(bm):
    assert bm.update_temperature("z", "r", 23.0) is False
    await bm.set_boost("z", "r", CONFIG, temperature=22.0)
    assert bm.update_temperature("z", "r", 23.0) is True
    assert bm.get_boost_info("z", "r", NOW)["temperature"] == 23.0


async def test_clear_boost(bm):
    assert bm.clear_boost("z", "r") is False
    await bm.set_boost("z", "r", CONFIG, temperature=22.0)
    assert bm.clear_boost("z", "r") is True
    assert bm.boost_state == {}


async def test_storage_round_trip_drops_expired(hass, bm, freezer):
    await bm.set_boost("z", "r", CONFIG, duration=60, temperature=22.0)
    stored = bm.get_state_for_storage()
    stored["z"]["old"] = {"temperature": 25.0, "end_time": (NOW - timedelta(minutes=1)).isoformat(), "duration": 30}
    restored = BoostManager(hass, 30)
    restored.restore_state(stored)
    assert restored.get_boost_info("z", "r", NOW)["temperature"] == 22.0
    assert restored.get_boost_info("z", "old", NOW) is None
