"""Button platform for Heating Manager: boost a room, or cancel its boost."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HeatingRoomEntity, async_add_room_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up boost and cancel boost buttons for every room."""
    async_add_room_entities(
        hass, entry, async_add_entities, Platform.BUTTON, [BoostButton, CancelBoostButton]
    )


class BoostButton(HeatingRoomEntity, ButtonEntity):
    """Boost the room for its boost duration, to its default boost temperature.

    Unavailable for rooms without temperature sensors, which can't be boosted.
    """

    key = "boost"

    @property
    def available(self) -> bool:
        return super().available and self._can_boost

    async def async_press(self) -> None:
        await self.coordinator.set_boost(self._zone_id, self._room_id)


class CancelBoostButton(HeatingRoomEntity, ButtonEntity):
    """End the room's boost (does nothing if it isn't boosted)."""

    key = "cancel_boost"

    @property
    def available(self) -> bool:
        return super().available and self._can_boost

    async def async_press(self) -> None:
        await self.coordinator.clear_boost(self._zone_id, self._room_id)
