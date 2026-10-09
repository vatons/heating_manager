"""Shared helpers for the per-room boost entities (sensor, button, number)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .climate import _room_device_info
from .const import CONF_SENSORS
from .coordinator import HeatingManagerCoordinator
from .entry_data import room_unique_id


@callback
def async_remove_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, domain: str, unique_ids: set[str | None]
) -> None:
    """Remove this platform's entities for rooms that no longer exist."""
    ent_reg = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if entity.domain == domain and entity.unique_id not in unique_ids:
            ent_reg.async_remove(entity.entity_id)


@callback
def async_add_room_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    domain: str,
    factories: list[type[HeatingRoomEntity]],
) -> None:
    """Add one entity of each kind for every room, each under its zone's subentry."""
    coordinator: HeatingManagerCoordinator = entry.runtime_data
    added: list[tuple[str | None, Entity]] = []
    for zone_id, zone in coordinator.config.get("zones", {}).items():
        if not isinstance(zone, dict):
            continue
        for room_id in zone.get("rooms", {}):
            for factory in factories:
                added.append((zone.get("subentry_id"), factory(coordinator, zone_id, room_id)))
    async_remove_stale_entities(hass, entry, domain, {e.unique_id for _, e in added})
    for subentry_id, entity in added:
        async_add_entities([entity], config_subentry_id=subentry_id)


class HeatingRoomEntity(CoordinatorEntity[HeatingManagerCoordinator]):
    """An entity on a room's device, next to the room's climate entity."""

    _attr_has_entity_name = True
    key: str

    def __init__(self, coordinator: HeatingManagerCoordinator, zone_id: str, room_id: str) -> None:
        super().__init__(coordinator)
        self._zone_id = zone_id
        self._room_id = room_id
        self._attr_translation_key = self.key
        self._attr_unique_id = f"{room_unique_id(room_id)}:{self.key}"
        self._attr_device_info = _room_device_info(coordinator, zone_id, room_id)

    @property
    def _room_config(self) -> dict:
        zone = self.coordinator.config.get("zones", {}).get(self._zone_id, {})
        return zone.get("rooms", {}).get(self._room_id, {})

    @property
    def _room_data(self) -> dict:
        return (self.coordinator.data or {}).get(self._zone_id, {}).get("rooms", {}).get(self._room_id, {})

    @property
    def _can_boost(self) -> bool:
        """The integration boosts only rooms with temperature sensors."""
        return bool(self._room_config.get(CONF_SENSORS))
