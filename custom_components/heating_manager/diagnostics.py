"""Diagnostics: settings, learned analytics and each room's log of heated stretches."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {"settings": dict(entry.options.get("settings", {})), "zones": {}}
    analytics = coordinator.heating_analytics
    zones: dict[str, Any] = {}
    for zone_id, zone_config in coordinator.config.get("zones", {}).items():
        zone_data = (coordinator.data or {}).get(zone_id, {})
        rooms: dict[str, Any] = {}
        for room_id, room_config in zone_config.get("rooms", {}).items():
            room_data = zone_data.get("rooms", {}).get(room_id, {})
            learned = None
            log: list[dict[str, Any]] = []
            if analytics is not None and (room := analytics.rooms.get(zone_id, {}).get(room_id)) is not None:
                learned = {key: getattr(room, key) for key in room.LEARNED}
                log = analytics.get_log(zone_id, room_id)
            rooms[room_id] = {
                "name": room_config.get("name", room_id),
                "trvs": len(room_config.get("trvs", [])),
                "sensors": len(room_config.get("sensors", [])),
                "temperature": room_data.get("temperature"),
                "temperature_source": room_data.get("temperature_source"),
                "target_temperature": room_data.get("target_temperature"),
                "learned": learned,
                "heated_stretches": log,
            }
        zones[zone_id] = {
            "name": zone_config.get("name", zone_id),
            "heat_source": zone_config.get("heat_source"),
            "heat_flowing": zone_data.get("heat_flowing"),
            "heat_flowing_basis": zone_data.get("heat_flowing_basis"),
            "rooms": rooms,
        }
    return {
        "version": entry.version,
        "settings": dict(entry.options.get("settings", {})),
        "outdoor_temperature": coordinator.outdoor_temperature,
        "away_mode": coordinator.away_mode,
        "zones": zones,
    }
