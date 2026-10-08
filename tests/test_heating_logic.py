"""Tests for HeatingLogic (room heating need and zone demand)."""
import pytest

from custom_components.heating_manager.const import (
    HEATING_DEMAND_MODE_ANY_ROOM,
    HEATING_DEMAND_MODE_ZONE_AVERAGE,
)
from custom_components.heating_manager.heating_logic import HeatingLogic


@pytest.fixture
def logic():
    return HeatingLogic(heating_deadband=0.3)


def need(logic, temp, target=20.0, room="r"):
    return logic.calculate_heating_need("z", room, temp, target)


# ---------------------------------------------------------------------------
# calculate_heating_need
# ---------------------------------------------------------------------------

def test_none_inputs_do_not_need_heating(logic):
    assert logic.calculate_heating_need("z", "r", None, 20.0) is False
    assert logic.calculate_heating_need("z", "r", 18.0, None) is False


def test_cold_room_needs_heating(logic):
    assert need(logic, 18.0) is True


def test_before_target_reached_uses_minimal_deadband(logic):
    # 19.85 is within 0.3 of target but outside the 0.1 minimal deadband
    assert need(logic, 19.85) is True


def test_target_reached_then_full_deadband_applies(logic):
    assert need(logic, 18.0) is True
    assert need(logic, 19.95) is False          # reached (within 0.1)
    assert need(logic, 19.8) is False           # within 0.3 deadband: coast
    assert need(logic, 19.69) is True           # below target - deadband


def test_target_change_resets_to_minimal_deadband(logic):
    need(logic, 20.0)                            # reached
    assert need(logic, 19.8) is False
    # Target raised: should heat immediately with minimal deadband
    assert need(logic, 19.8, target=20.5) is True


def test_tiny_target_change_is_ignored(logic):
    need(logic, 20.0)
    assert logic.room_heating_state["z"]["r"]["target_reached"] is True
    need(logic, 19.95, target=20.05)
    assert logic.room_heating_state["z"]["r"]["target_reached"] is True


def test_rooms_tracked_independently(logic):
    need(logic, 20.0, room="a")
    assert need(logic, 19.8, room="a") is False
    assert need(logic, 19.8, room="b") is True


def test_heating_need_state_persists_round_trip(logic):
    need(logic, 20.0)
    stored = logic.get_state_for_storage()
    restored = HeatingLogic(0.3)
    restored.restore_state(stored)
    assert restored.calculate_heating_need("z", "r", 19.8, 20.0) is False


def test_restore_state_accepts_legacy_flat_format():
    logic = HeatingLogic(0.3)
    logic.restore_state({"z": {"r": {"previous_target": 20.0, "target_reached": True}}})
    assert logic.calculate_heating_need("z", "r", 19.8, 20.0) is False


# ---------------------------------------------------------------------------
# calculate_zone_heating_demand
# ---------------------------------------------------------------------------

def room(temp, target, needs=False, boost=None):
    return {"temperature": temp, "target_temperature": target, "needs_heating": needs, "boost": boost}


def test_any_room_mode(logic):
    rooms = {"a": room(20, 20, False), "b": room(18, 20, True)}
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ANY_ROOM, "z") is True
    rooms["b"]["needs_heating"] = False
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ANY_ROOM, "z") is False


def test_unknown_mode_falls_back_to_any_room(logic):
    rooms = {"a": room(18, 20, True)}
    assert logic.calculate_zone_heating_demand(rooms, "nonsense", "z") is True


def test_boost_always_demands_heat(logic):
    rooms = {"a": room(25, 20, False, boost={"temperature": 22})}
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is True


def reach_target(logic, zone="z", target=20):
    """Put a zone in the 'target reached' state, where the full deadband applies."""
    rooms = {"a": room(target, target)}
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ZONE_AVERAGE, zone) is False


def test_zone_average_hysteresis(logic):
    mode = HEATING_DEMAND_MODE_ZONE_AVERAGE
    reach_target(logic)
    rooms = {"a": room(19.8, 20), "b": room(19.8, 20)}
    assert logic.calculate_zone_heating_demand(rooms, mode, "z") is False   # within deadband
    rooms = {"a": room(19.5, 20), "b": room(19.6, 20)}
    assert logic.calculate_zone_heating_demand(rooms, mode, "z") is True    # below deadband
    rooms = {"a": room(19.8, 20), "b": room(19.8, 20)}
    assert logic.calculate_zone_heating_demand(rooms, mode, "z") is True    # stays on
    rooms = {"a": room(20.0, 20), "b": room(20.0, 20)}
    assert logic.calculate_zone_heating_demand(rooms, mode, "z") is False   # reached


def test_zone_average_hysteresis_is_per_zone(logic):
    mode = HEATING_DEMAND_MODE_ZONE_AVERAGE
    reach_target(logic, "z1")
    reach_target(logic, "z2")
    assert logic.calculate_zone_heating_demand({"a": room(19, 20)}, mode, "z1") is True
    assert logic.calculate_zone_heating_demand({"a": room(19.8, 20)}, mode, "z2") is False
    assert logic.calculate_zone_heating_demand({"a": room(19.8, 20)}, mode, "z1") is True


def test_zone_average_ignores_rooms_without_temperature(logic):
    rooms = {"a": room(None, 20), "b": room(18, 20)}
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is True


def test_zone_average_no_data_is_no_demand(logic):
    rooms = {"a": room(None, 20)}
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is False


def test_zone_average_hides_a_cold_room(logic):
    """Documents a property of zone_average: one cold room can be outvoted.

    Bedroom at 15.5°C (4.5° under) is masked by two warm rooms, so the boiler
    never fires for it. This is by design of the mode but worth knowing.
    """
    reach_target(logic)
    rooms = {"a": room(22.0, 20), "b": room(22.0, 20), "c": room(15.5, 20, True)}
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is False


def test_zone_demand_state_persists_round_trip(logic):
    mode = HEATING_DEMAND_MODE_ZONE_AVERAGE
    logic.calculate_zone_heating_demand({"a": room(19, 20)}, mode, "z")
    restored = HeatingLogic(0.3)
    restored.restore_state(logic.get_state_for_storage())
    assert restored.calculate_zone_heating_demand({"a": room(19.8, 20)}, mode, "z") is True


def test_off_rooms_ignored_for_demand_even_when_boosted(logic):
    rooms = {"a": {**room(15, 20, True, boost={"temperature": 22}), "off": True}, "b": room(20, 20)}
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ANY_ROOM, "z") is False
    assert logic.calculate_zone_heating_demand(rooms, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is False


# ---------------------------------------------------------------------------
# zone_average smart deadband after a target change
# ---------------------------------------------------------------------------

def test_zone_average_small_target_increase_fires(logic):
    """Average 0.2°C under a new target: heat (minimal deadband), not wait for 0.3°C."""
    mode = HEATING_DEMAND_MODE_ZONE_AVERAGE
    reach_target(logic, target=17.5)
    assert logic.calculate_zone_heating_demand({"a": room(19.8, 20)}, mode, "z") is True
    # Stays on until the target is reached, then coasts within the full deadband
    assert logic.calculate_zone_heating_demand({"a": room(19.95, 20)}, mode, "z") is True
    assert logic.calculate_zone_heating_demand({"a": room(20.0, 20)}, mode, "z") is False
    assert logic.calculate_zone_heating_demand({"a": room(19.8, 20)}, mode, "z") is False
    assert logic.calculate_zone_heating_demand({"a": room(19.69, 20)}, mode, "z") is True


def test_zone_average_within_minimal_deadband_does_not_fire(logic):
    reach_target(logic, target=17.5)
    assert logic.calculate_zone_heating_demand({"a": room(19.95, 20)}, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is False


def test_zone_average_target_decrease_does_not_fire(logic):
    reach_target(logic, target=20)
    assert logic.calculate_zone_heating_demand({"a": room(20.0, 17.5)}, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is False


def test_zone_average_target_state_persists(logic):
    mode = HEATING_DEMAND_MODE_ZONE_AVERAGE
    reach_target(logic, target=20)
    restored = HeatingLogic(0.3)
    restored.restore_state(logic.get_state_for_storage())
    # Reached state survives a restart: full deadband still applies
    assert restored.calculate_zone_heating_demand({"a": room(19.8, 20)}, mode, "z") is False


def test_zone_average_restores_storage_without_target_state(logic):
    """Storage written before this change has no zone_avg_target_state."""
    restored = HeatingLogic(0.3)
    restored.restore_state({"room_heating_state": {}, "zone_avg_heating_active": {"z": False}})
    assert restored.calculate_zone_heating_demand({"a": room(19.8, 20)}, HEATING_DEMAND_MODE_ZONE_AVERAGE, "z") is True
