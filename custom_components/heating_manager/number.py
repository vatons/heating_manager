"""Number platform for Heating Manager: each room's boost duration."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MAX_BOOST_DURATION, MIN_BOOST_DURATION
from .entity import HeatingRoomEntity, async_add_room_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a boost duration number for every room."""
    async_add_room_entities(hass, entry, async_add_entities, Platform.NUMBER, [BoostDurationNumber])


class BoostDurationNumber(HeatingRoomEntity, NumberEntity):
    """How long a boost lasts in this room when no duration is given.

    Starts at the integration's boost duration (Configure → Settings); changing
    it here sets this room's own duration. Used by the boost button, the boost
    preset and set_boost without a duration.
    """

    key = "boost_duration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_BOOST_DURATION
    _attr_native_max_value = MAX_BOOST_DURATION
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    @property
    def available(self) -> bool:
        return super().available and self._can_boost

    @property
    def native_value(self) -> int:
        return self.coordinator.get_boost_duration(self._zone_id, self._room_id)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.set_room_boost_duration(self._zone_id, self._room_id, int(value))
