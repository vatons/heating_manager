"""Button platform for Heating Manager: boost a room or cancel its boost, and skip a room
or zone to its next scheduled temperature."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import HeatingRoomEntity, HeatingZoneEntity, async_add_room_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up boost, cancel boost and skip buttons for every room, and skip for every zone."""
    async_add_room_entities(
        hass, entry, async_add_entities, Platform.BUTTON,
        [BoostButton, CancelBoostButton, RoomSkipButton], [ZoneSkipButton],
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


def _can_skip(coordinator, zone_data: dict) -> bool:
    """A change to skip to, and not in away mode (which outranks it)."""
    return not coordinator.away_mode and bool((zone_data.get("schedule_info") or {}).get("next_period"))


class RoomSkipButton(HeatingRoomEntity, ButtonEntity):
    """Go to the room's next scheduled temperature now, until that change comes."""

    key = "skip_to_next"

    @property
    def available(self) -> bool:
        zone_data = (self.coordinator.data or {}).get(self._zone_id, {})
        return super().available and _can_skip(self.coordinator, zone_data)

    async def async_press(self) -> None:
        await self.coordinator.skip_to_next(self._zone_id, self._room_id)


class ZoneSkipButton(HeatingZoneEntity, ButtonEntity):
    """Go to the zone's next scheduled temperature now (every room in it)."""

    key = "skip_to_next"

    @property
    def available(self) -> bool:
        return super().available and _can_skip(self.coordinator, self._zone_data)

    async def async_press(self) -> None:
        await self.coordinator.skip_to_next(self._zone_id)
