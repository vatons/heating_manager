"""Sensor platform for Heating Manager: when each room's boost ends."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HeatingRoomEntity, async_add_room_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a boost end sensor for every room."""
    async_add_room_entities(hass, entry, async_add_entities, Platform.SENSOR, [BoostEndSensor])


class BoostEndSensor(HeatingRoomEntity, SensorEntity):
    """When the room's boost ends; unknown when it isn't boosted.

    A timestamp, so dashboards show it as "in 25 minutes".
    """

    key = "boost_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        boost = self._room_data.get("boost")
        return boost.get("end_time") if boost else None
