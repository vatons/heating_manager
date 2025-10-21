"""TRV control management for Heating Manager."""
import logging

from homeassistant.core import HomeAssistant

from .const import CONF_TRVS
from .trv_controller import TRVController

_LOGGER = logging.getLogger(__name__)


class TRVManager:
    """Manages TRV temperature control and offset monitoring."""

    def __init__(self, trv_controller: TRVController) -> None:
        """Initialize the TRV manager.

        Args:
            trv_controller: The TRV controller instance for intelligent setpoint calculations
        """
        self.trv_controller = trv_controller

    async def set_trv_temperatures(
        self,
        zone_id: str,
        room_id: str,
        room_config: dict,
        target_temp: float | None,
        room_temp: float | None,
        needs_heating: bool,
    ) -> None:
        """Set the target temperature for all TRVs in a room using intelligent control."""
        if target_temp is None:
            _LOGGER.warning(
                "Skipping TRV temperature update: target_temp is None for room %s",
                room_config.get("name", "unknown"),
            )
            return

        trvs = room_config.get(CONF_TRVS, [])

        for trv_id in trvs:
            await self.trv_controller.set_trv_temperature(
                zone_id, room_id, trv_id,
                target_temp, room_temp, needs_heating
            )

    async def get_trv_offset_info(
        self,
        hass: HomeAssistant,
        zone_id: str,
        room_id: str,
        room_config: dict,
        room_temp: float | None,
    ) -> dict:
        """Get TRV offset information for display in entity attributes.

        Returns dict with TRV internal temps, offsets, and setpoints for monitoring.
        """
        trvs = room_config.get(CONF_TRVS, [])

        trv_data = {}
        for trv_id in trvs:
            trv_state = hass.states.get(trv_id)
            trv_internal_temp = None

            if trv_state and trv_state.attributes.get("current_temperature"):
                try:
                    trv_internal_temp = float(trv_state.attributes["current_temperature"])
                except (ValueError, TypeError):
                    pass

            # Calculate current offset
            current_offset = None
            if trv_internal_temp is not None and room_temp is not None:
                current_offset = trv_internal_temp - room_temp

            # Get learned EMA offset
            avg_offset = None
            if self.trv_controller.enabled:
                avg_offset = self.trv_controller._get_ema_offset(zone_id, room_id, trv_id)

            # Get last setpoint from TRV state
            trv_setpoint = None
            if trv_state and trv_state.attributes.get("temperature"):
                try:
                    trv_setpoint = float(trv_state.attributes["temperature"])
                except (ValueError, TypeError):
                    pass

            trv_data[trv_id] = {
                "trv_internal_temp": trv_internal_temp,
                "current_offset": current_offset,
                "average_offset": avg_offset,
                "trv_setpoint": trv_setpoint,
            }

        return trv_data
