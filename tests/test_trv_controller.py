"""Tests for TRVController (setpoint calculation) and TRV command delivery."""
import pytest

from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant

from custom_components.heating_manager.const import TRV_MIN_SETPOINT
from custom_components.heating_manager.trv_controller import TRVController
from custom_components.heating_manager.trv_manager import TRVManager

from .conftest import FakeTRV

TRV = "climate.room_trv"


@pytest.fixture
def ctrl(hass):
    return TRVController(hass)


def setpoint(ctrl, room, target, internal, needs, trv=TRV):
    return ctrl.calculate_trv_setpoint("z", "r", trv, room, target, internal, needs)


# ---------------------------------------------------------------------------
# Setpoint calculation
# ---------------------------------------------------------------------------

def test_disabled_controller_sends_plain_target(hass):
    ctrl = TRVController(hass, enabled=False)
    assert setpoint(ctrl, 15.0, 20.0, 25.0, True) == 20.0


def test_missing_internal_temperature_sends_plain_target(ctrl):
    assert setpoint(ctrl, 18.0, 20.0, None, True) == 20.0


@pytest.mark.parametrize(
    ("room", "expected_boost"),
    [
        (16.5, 5.0),     # deficit 3.5  -> large: max boost
        (18.0, 3.0),     # deficit 2.0  -> medium: 2.0 * 1.5
        (19.0, 1.5),     # deficit 1.0  -> small: fixed 1.5
        (19.8, 0.5),     # deficit 0.2  -> tiny
    ],
)
def test_deficit_bands(ctrl, room, expected_boost):
    # Internal == room => offset 0
    assert setpoint(ctrl, room, 20.0, room, True) == pytest.approx(20.0 + expected_boost)


def test_maintain_when_not_needing_heat(ctrl):
    assert setpoint(ctrl, 20.0, 20.0, 22.0, False) == pytest.approx(20.0 + 2.0 + 0.5)


def test_overshoot_sets_trv_low(ctrl):
    assert setpoint(ctrl, 20.5, 20.0, 20.5, False) == pytest.approx(19.0)


def test_overshoot_never_below_floor(ctrl):
    assert setpoint(ctrl, 7.0, 5.0, 7.0, False) == TRV_MIN_SETPOINT


def test_setpoint_clamped_to_absolute_max(ctrl):
    assert setpoint(ctrl, 10.0, 28.0, 20.0, True) == 30.0


def test_positive_offset_raises_setpoint(ctrl):
    # TRV internal reads 3° warm: setpoint is lifted by the offset
    assert setpoint(ctrl, 18.0, 20.0, 21.0, True) == pytest.approx(20.0 + 3.0 + 3.0)


def test_ema_initialises_then_smooths(hass):
    ctrl = TRVController(hass, ema_alpha=0.5)
    setpoint(ctrl, 18.0, 20.0, 20.0, True)          # offset 2
    assert ctrl._get_ema_offset("z", "r", TRV) == pytest.approx(2.0)
    setpoint(ctrl, 18.0, 20.0, 22.0, True)          # offset 4
    assert ctrl._get_ema_offset("z", "r", TRV) == pytest.approx(3.0)


def test_ema_is_tracked_per_trv(ctrl):
    setpoint(ctrl, 18.0, 20.0, 20.0, True, trv="climate.a")
    setpoint(ctrl, 18.0, 20.0, 18.5, True, trv="climate.b")
    assert ctrl._get_ema_offset("z", "r", "climate.a") == pytest.approx(2.0)
    assert ctrl._get_ema_offset("z", "r", "climate.b") == pytest.approx(0.5)


def test_unknown_trv_uses_default_offset(ctrl):
    assert ctrl._get_ema_offset("z", "r", "climate.never_seen") == 0.0


def test_offset_history_round_trip(hass, ctrl):
    setpoint(ctrl, 18.0, 20.0, 20.0, True)
    restored = TRVController(hass)
    restored.restore_offset_history(ctrl.get_offset_history_for_storage())
    assert restored._get_ema_offset("z", "r", TRV) == pytest.approx(2.0)


def test_legacy_list_history_converted_to_mean(hass):
    ctrl = TRVController(hass)
    ctrl.restore_offset_history({"z": {"r": {TRV: [1.0, 2.0, 3.0]}}})
    assert ctrl._get_ema_offset("z", "r", TRV) == pytest.approx(2.0)


def test_legacy_detection_handles_empty_first_room(hass):
    """An empty room dict before a list-format room must not hide the legacy format."""
    ctrl = TRVController(hass)
    ctrl.restore_offset_history({"z": {"empty": {}, "r": {TRV: [1.0, 3.0]}}})
    assert ctrl._get_ema_offset("z", "r", TRV) == pytest.approx(2.0)


@pytest.mark.xfail(
    reason="BUG: EMA alpha 0.15/min forgets in ~6 min, so the 'learned' offset "
    "chases radiator heat instead of learning the TRV's sensor bias"
)
def test_learned_offset_is_stable_during_radiator_warm_up(ctrl):
    # Hours of steady state with a 1°C bias
    for _ in range(240):
        setpoint(ctrl, 19.0, 20.0, 20.0, True)
    # Radiator warms up: TRV internal sensor jumps 4°C for 10 minutes
    for _ in range(10):
        setpoint(ctrl, 19.0, 20.0, 24.0, True)
    assert ctrl._get_ema_offset("z", "r", TRV) < 2.0


@pytest.mark.xfail(
    reason="BUG: with no room temperature the learned offset is ignored, so a TRV "
    "with a warm internal sensor closes even though the room is cold"
)
def test_unknown_room_temperature_still_applies_learned_offset(ctrl):
    for _ in range(50):
        setpoint(ctrl, 18.0, 20.0, 21.0, True)       # learn a +3 offset
    # Room sensor drops out; TRV internal still reads 21
    sp = setpoint(ctrl, None, 20.0, 21.0, True)
    assert sp > 21.0, "TRV must stay open while room needs heat"


# ---------------------------------------------------------------------------
# Command delivery to real (fake) climate entities
# ---------------------------------------------------------------------------

async def test_sends_setpoint_to_trv(hass: HomeAssistant, add_trvs, ctrl):
    trvs = await add_trvs(FakeTRV("room_trv", current_temperature=18.0))
    await ctrl.set_trv_temperature("z", "r", TRV, 20.0, 18.0, True)
    assert trvs[TRV].set_temperature_calls == [pytest.approx(23.0)]
    assert trvs[TRV].valve_open


async def test_reads_internal_temperature_from_trv(hass, add_trvs, ctrl):
    await add_trvs(FakeTRV("room_trv", current_temperature=21.0))
    await ctrl.set_trv_temperature("z", "r", TRV, 20.0, 18.0, True)
    assert ctrl._get_ema_offset("z", "r", TRV) == pytest.approx(3.0)


async def test_trv_errors_do_not_propagate(hass, add_trvs, ctrl):
    await add_trvs(FakeTRV("room_trv", fail_with=RuntimeError("zigbee timeout")))
    await ctrl.set_trv_temperature("z", "r", TRV, 20.0, 18.0, True)   # must not raise


async def test_missing_trv_entity_does_not_raise(hass, add_trvs, ctrl):
    await add_trvs(FakeTRV("other_trv"))
    await ctrl.set_trv_temperature("z", "r", "climate.does_not_exist", 20.0, 18.0, True)


async def test_trv_manager_skips_when_target_none(hass, add_trvs, ctrl):
    trvs = await add_trvs(FakeTRV("room_trv"))
    await TRVManager(ctrl).set_trv_temperatures("z", "r", {"trvs": [TRV]}, None, 18.0, True)
    assert trvs[TRV].set_temperature_calls == []


async def test_trv_manager_commands_every_trv_in_room(hass, add_trvs, ctrl):
    trvs = await add_trvs(FakeTRV("a"), FakeTRV("b"))
    await TRVManager(ctrl).set_trv_temperatures(
        "z", "r", {"trvs": ["climate.a", "climate.b"]}, 20.0, 18.0, True
    )
    assert trvs["climate.a"].set_temperature_calls and trvs["climate.b"].set_temperature_calls


async def test_offset_info_reports_trv_state(hass, add_trvs, ctrl):
    await add_trvs(FakeTRV("room_trv", current_temperature=21.0, target_temperature=23.0))
    info = await TRVManager(ctrl).get_trv_offset_info(hass, "z", "r", {"trvs": [TRV]}, 18.0)
    assert info[TRV]["trv_internal_temp"] == 21.0
    assert info[TRV]["current_offset"] == pytest.approx(3.0)
    assert info[TRV]["trv_setpoint"] == 23.0


@pytest.mark.xfail(
    reason="BUG: setpoint above the TRV's max_temp is rejected by HA and the error "
    "swallowed, leaving the TRV at its previous (possibly closed) setpoint"
)
async def test_setpoint_above_trv_max_is_clamped_not_dropped(hass, add_trvs, ctrl):
    # Cold room (deficit 4.5 -> +5 boost) and a TRV limited to 25°C, currently closed
    trvs = await add_trvs(
        FakeTRV("room_trv", current_temperature=17.0, target_temperature=9.0, max_temp=25.0)
    )
    await ctrl.set_trv_temperature("z", "r", TRV, 19.5, 15.0, True)
    trv = trvs[TRV]
    assert trv.target_temperature == 25.0
    assert trv.valve_open


@pytest.mark.xfail(
    reason="BUG: setpoint below the TRV's min_temp is rejected by HA and dropped, "
    "so the TRV stays open when it should close"
)
async def test_setpoint_below_trv_min_is_clamped_not_dropped(hass, add_trvs, ctrl):
    # Room overshooting a 5°C frost target -> controller asks for 5°C; TRV min is 7
    trvs = await add_trvs(
        FakeTRV("room_trv", current_temperature=20.0, target_temperature=24.0, min_temp=7.0)
    )
    await ctrl.set_trv_temperature("z", "r", TRV, 5.0, 20.0, False)
    assert trvs[TRV].target_temperature == 7.0
    assert not trvs[TRV].valve_open


@pytest.mark.xfail(
    reason="BUG: TRV hvac_mode is never checked; a TRV in OFF ignores every setpoint"
)
async def test_trv_in_off_mode_is_switched_to_heat(hass, add_trvs, ctrl):
    trvs = await add_trvs(
        FakeTRV("room_trv", current_temperature=16.0, hvac_mode=HVACMode.OFF)
    )
    await ctrl.set_trv_temperature("z", "r", TRV, 20.0, 16.0, True)
    assert trvs[TRV].hvac_mode == HVACMode.HEAT
    assert trvs[TRV].valve_open


@pytest.mark.xfail(reason="BUG: setpoints are not rounded to the TRV's target_temperature_step")
async def test_setpoint_respects_trv_step(hass, add_trvs, ctrl):
    trvs = await add_trvs(
        FakeTRV("room_trv", current_temperature=18.3, target_temperature_step=0.5)
    )
    await ctrl.set_trv_temperature("z", "r", TRV, 19.5, 17.9, True)
    sent = trvs[TRV].set_temperature_calls[-1]
    assert sent * 2 == round(sent * 2)
