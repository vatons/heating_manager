"""Tests for UI setup: config flow, options flow, YAML import and migration."""
from __future__ import annotations

import pytest
import yaml

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heating_manager.const import DOMAIN
from custom_components.heating_manager.entry_data import (
    default_settings,
    new_options,
    options_to_runtime,
    period_minutes,
    unique_id_for,
    yaml_to_options,
)

from .conftest import FakeTRV, local_dt, set_temp

ROOM_OPTIONS = {
    "name": "Lounge",
    "trvs": ["climate.lounge_trv"],
    "sensors": [{"temperature": "sensor.lounge", "last_seen": "sensor.lounge_last_seen"}],
}


def base_options(**settings) -> dict:
    options = new_options("Downstairs")
    options["zones"]["downstairs"]["rooms"]["lounge"] = dict(ROOM_OPTIONS)
    options["settings"].update(settings)
    return options


@pytest.fixture
async def env(hass: HomeAssistant, add_trvs, freezer):
    freezer.move_to(local_dt(hour=12))
    trvs = await add_trvs(
        FakeTRV("lounge_trv", current_temperature=17.0),
        FakeTRV("study_trv", current_temperature=17.0),
    )
    set_temp(hass, "sensor.lounge", 17.0)
    set_temp(hass, "sensor.study", 17.0)
    yield trvs
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def entry(hass, env):
    config_entry = MockConfigEntry(domain=DOMAIN, title="Heating Manager", data={}, options=base_options())
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def menu(hass, flow_id, step):
    return await hass.config_entries.options.async_configure(flow_id, {"next_step_id": step})


async def form(hass, flow_id, data):
    return await hass.config_entries.options.async_configure(flow_id, data)


async def save(hass, flow_id, choice_field=None):
    """Save from a menu (default) or from a list form via its __save__ choice."""
    if choice_field:
        result = await form(hass, flow_id, {choice_field: "__save__"})
    else:
        result = await menu(hass, flow_id, "save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result


def device_ids(hass, entry) -> set[str]:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {ident for device in devices for domain, ident in device.identifiers if domain == DOMAIN}


async def open_options(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    return result["flow_id"]


# ---------------------------------------------------------------------------
# Add Integration
# ---------------------------------------------------------------------------

async def test_user_flow_creates_first_zone(hass, env):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "  "})
    assert result["errors"] == {"name": "name_required"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Upstairs"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    options = result["result"].options
    assert options["settings"] == default_settings()
    zone = options["zones"]["upstairs"]
    assert zone["name"] == "Upstairs"
    assert zone["schedule"]["weekday"] == [{"start": "06:30", "end": "22:00", "temperature": 19.0}]
    assert hass.states.get("climate.upstairs") is not None
    assert hass.states.get("climate.heating_manager") is not None


async def test_only_one_instance(hass, entry):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_set_mode_without_entry_raises(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "set_mode", {"mode": "away"}, blocking=True)


# ---------------------------------------------------------------------------
# YAML import and migration
# ---------------------------------------------------------------------------

LEGACY_YAML = """
minimum_temp: 12
heating_demand_mode: zone_average
zones:
  downstairs:
    name: Downstairs
    heating_demand_mode: any_room
    schedule:
      weekday:
        - {start: 16:00, end: 21:00, temperature: 20}
        - {start: 06:30, end: "08:00", temperature: 19.5}
        - {start: "bad", end: "08:00", temperature: 19.5}
      weekend:
        - {start: "07:00", end: "24:00", temperature: 20}
    rooms:
      lounge:
        name: Lounge
        temperature_offset: -1.0
        trvs: [climate.lounge_trv]
        sensors:
          - temperature: sensor.lounge
            last_seen: sensor.lounge_last_seen
      study:
        trvs: [climate.study_trv]
        sensors: [sensor.study]
"""


async def setup_yaml(hass, tmp_path, text=LEGACY_YAML, **conf):
    path = tmp_path / "heating_manager.yaml"
    path.write_text(text)
    ok = await async_setup_component(hass, DOMAIN, {DOMAIN: {"config_file": str(path), **conf}})
    await hass.async_block_till_done()
    return ok, str(path)


async def test_yaml_import_creates_entry_and_repair_issue(hass, env, tmp_path):
    ok, path = await setup_yaml(hass, tmp_path, boost_duration=45)
    assert ok
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    options = entries[0].options

    assert options["settings"]["minimum_temp"] == 12
    assert options["settings"]["heating_demand_mode"] == "zone_average"
    assert options["settings"]["boost_duration"] == 45          # from configuration.yaml
    zone = options["zones"]["downstairs"]
    assert zone["heating_demand_mode"] == "any_room"
    assert zone["schedule"]["weekday"] == [                     # sorted, normalised, invalid dropped
        {"start": "06:30", "end": "08:00", "temperature": 19.5},
        {"start": "16:00", "end": "21:00", "temperature": 20.0},
    ]
    assert zone["schedule"]["weekend"] == [{"start": "07:00", "end": "00:00", "temperature": 20.0}]
    assert zone["rooms"]["lounge"]["temperature_offset"] == -1.0
    assert zone["rooms"]["lounge"]["sensors"] == [
        {"temperature": "sensor.lounge", "last_seen": "sensor.lounge_last_seen"}
    ]
    assert zone["rooms"]["study"] == {
        "name": "study", "trvs": ["climate.study_trv"], "sensors": [{"temperature": "sensor.study"}],
    }

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "yaml_imported")
    assert issue is not None
    assert issue.translation_placeholders == {"config_file": path}


async def test_yaml_ignored_once_imported(hass, env, tmp_path):
    existing = MockConfigEntry(domain=DOMAIN, data={}, options=base_options(minimum_temp=15))
    existing.add_to_hass(hass)
    ok, _ = await setup_yaml(hass, tmp_path)
    assert ok
    entries = hass.config_entries.async_entries(DOMAIN)
    assert entries == [existing]
    assert existing.options["settings"]["minimum_temp"] == 15
    assert ir.async_get(hass).async_get_issue(DOMAIN, "yaml_imported") is not None


async def test_import_flow_aborts_when_configured(hass, entry):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_IMPORT}, data=base_options()
    )
    assert result["type"] is FlowResultType.ABORT


@pytest.mark.parametrize("text", ["", "- just\n- a list\n"])
async def test_yaml_not_a_mapping_fails_setup(hass, env, tmp_path, text):
    ok, _ = await setup_yaml(hass, tmp_path, text=text)
    assert not ok
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_migration_keeps_entity_ids_and_state(hass, hass_storage, env, tmp_path):
    """Entities created by the old YAML platform keep their entity_ids after import."""
    ent_reg = er.async_get(hass)
    for unique_id, object_id in [
        ("heating_manager_downstairs_lounge", "lounge_hm"),
        ("heating_manager_downstairs_zone", "downstairs_zone_hm"),
        ("heating_manager_global", "global_hm"),
    ]:
        ent_reg.async_get_or_create("climate", DOMAIN, unique_id, suggested_object_id=object_id)
    hass_storage["heating_manager.storage"] = {
        "version": 1, "minor_version": 1, "key": "heating_manager.storage",
        "data": {"version": 2, "manual_room_temp": {"downstairs": {"lounge": {
            "temperature": 22.0, "last_scheduled_temp": 12.0}}}},
    }

    await setup_yaml(hass, tmp_path)

    lounge = hass.states.get("climate.lounge_hm")
    assert lounge is not None
    assert lounge.attributes["temperature"] == 22.0          # override survived
    assert hass.states.get("climate.downstairs_zone_hm") is not None
    assert hass.states.get("climate.global_hm") is not None
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    assert ent_reg.async_get("climate.lounge_hm").config_entry_id == entry_id


# ---------------------------------------------------------------------------
# Configure menu: settings
# ---------------------------------------------------------------------------

async def test_basic_and_advanced_settings(hass, entry):
    flow_id = await open_options(hass, entry)

    result = await menu(hass, flow_id, "settings")
    assert result["step_id"] == "settings"
    keys = {str(k) for k in result["data_schema"].schema}
    assert "minimum_temp" in keys and "trv_overshoot_max" not in keys
    result = await form(hass, flow_id, {
        "minimum_temp": 14, "frost_protection_temp": 6, "heating_demand_mode": "zone_average",
        "fallback_mode": "trv", "heating_deadband": 0.5, "boost_duration": 60,
    })
    assert result["step_id"] == "init"

    result = await menu(hass, flow_id, "advanced")
    keys = {str(k) for k in result["data_schema"].schema}
    assert "trv_overshoot_max" in keys and "minimum_temp" not in keys
    result = await form(hass, flow_id, {**{k: v for k, v in default_settings().items() if k in keys}, "update_interval": 120})
    assert result["step_id"] == "init"

    await save(hass, flow_id)
    settings = entry.options["settings"]
    assert settings["minimum_temp"] == 14 and settings["fallback_mode"] == "trv"
    assert settings["update_interval"] == 120
    # The entry reloaded with the new settings
    coordinator = entry.runtime_data
    assert coordinator.minimum_temp == 14
    assert coordinator.update_interval.total_seconds() == 120
    assert coordinator.temperature_manager.fallback_mode == "trv"


async def test_closing_without_save_changes_nothing(hass, entry):
    flow_id = await open_options(hass, entry)
    await menu(hass, flow_id, "settings")
    await form(hass, flow_id, {
        "minimum_temp": 14, "frost_protection_temp": 6, "heating_demand_mode": "zone_average",
        "fallback_mode": "trv", "heating_deadband": 0.5, "boost_duration": 60,
    })
    hass.config_entries.options.async_abort(flow_id)
    assert entry.options["settings"]["minimum_temp"] == default_settings()["minimum_temp"]


# ---------------------------------------------------------------------------
# Configure menu: zones and rooms
# ---------------------------------------------------------------------------

async def test_add_zone_room_and_schedule(hass, entry, env):
    flow_id = await open_options(hass, entry)
    result = await menu(hass, flow_id, "zones")
    assert result["step_id"] == "zones"

    # Add a zone
    result = await form(hass, flow_id, {"zone": "__add__"})
    assert result["step_id"] == "zone_edit"
    result = await form(hass, flow_id, {"name": "", "heating_demand_mode": "global"})
    assert result["errors"] == {"name": "name_required"}
    result = await form(hass, flow_id, {"name": "Upstairs", "heating_demand_mode": "zone_average"})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "zone"

    # Add a room: validation first
    result = await menu(hass, flow_id, "rooms")
    result = await form(hass, flow_id, {"room": "__add__"})
    assert result["step_id"] == "room"
    result = await form(hass, flow_id, {"name": "Study", "trvs": [], "sensors": []})
    assert result["errors"] == {"base": "room_empty"}
    result = await form(hass, flow_id, {
        "name": "Study", "trvs": ["climate.study_trv"], "sensors": ["sensor.study"],
        "temperature_offset": -1.5, "configure_last_seen": True,
    })
    assert result["step_id"] == "room_last_seen"
    result = await form(hass, flow_id, {"sensor.study": "sensor.study_last_seen"})
    assert result["step_id"] == "rooms"
    result = await form(hass, flow_id, {"room": "__back__"})
    assert result["step_id"] == "zone"

    # Schedule: replace the default weekday period with two periods
    result = await menu(hass, flow_id, "schedule")
    result = await form(hass, flow_id, {"day": "weekday"})
    assert result["step_id"] == "schedule_day"
    result = await form(hass, flow_id, {"period": "0"})                       # edit default
    assert result["step_id"] == "period"
    result = await form(hass, flow_id, {"start": "06:00:00", "end": "08:00:00", "temperature": 20, "delete": False})
    assert result["step_id"] == "schedule_day"
    result = await form(hass, flow_id, {"period": "__add__"})
    result = await form(hass, flow_id, {"start": "07:30:00", "end": "09:00:00", "temperature": 18})
    assert result["errors"] == {"base": "overlap"}
    result = await form(hass, flow_id, {"start": "17:00:00", "end": "22:30:00", "temperature": 21})
    assert result["step_id"] == "schedule_day"
    # Weekend: copy weekdays
    result = await form(hass, flow_id, {"period": "__back__"})
    result = await form(hass, flow_id, {"day": "weekend"})
    result = await form(hass, flow_id, {"period": "__copy__"})
    assert result["step_id"] == "schedule_day"
    result = await form(hass, flow_id, {"period": "__back__"})
    result = await form(hass, flow_id, {"day": "__back__"})
    assert result["step_id"] == "zone"
    result = await menu(hass, flow_id, "zones")
    result = await form(hass, flow_id, {"zone": "__back__"})
    assert result["step_id"] == "init"
    await save(hass, flow_id)

    zone = entry.options["zones"]["upstairs"]
    assert zone["heating_demand_mode"] == "zone_average"
    assert zone["rooms"]["study"] == {
        "name": "Study",
        "trvs": ["climate.study_trv"],
        "sensors": [{"temperature": "sensor.study", "last_seen": "sensor.study_last_seen"}],
        "temperature_offset": -1.5,
    }
    expected = [
        {"start": "06:00", "end": "08:00", "temperature": 20.0},
        {"start": "17:00", "end": "22:30", "temperature": 21.0},
    ]
    assert zone["schedule"]["weekday"] == expected
    assert zone["schedule"]["weekend"] == expected

    study = hass.states.get("climate.upstairs_study")
    assert study is not None
    # 12:00 is outside both periods: minimum 7°C, offset -1.5 clamped at frost protection (7°C)
    assert study.attributes["temperature"] == 7.0
    assert "zone_upstairs" in device_ids(hass, entry)


async def test_edit_room_keeps_last_seen_and_deletes(hass, entry):
    flow_id = await open_options(hass, entry)
    await menu(hass, flow_id, "zones")
    await form(hass, flow_id, {"zone": "downstairs"})
    await menu(hass, flow_id, "rooms")
    result = await form(hass, flow_id, {"room": "lounge"})
    assert result["step_id"] == "room"
    result = await form(hass, flow_id, {
        "name": "Living room", "trvs": ["climate.lounge_trv"],
        "sensors": ["sensor.lounge", "sensor.study"], "temperature_offset": 0,
    })
    assert result["step_id"] == "rooms"
    await save(hass, flow_id, "room")
    room = entry.options["zones"]["downstairs"]["rooms"]["lounge"]       # id unchanged on rename
    assert room["name"] == "Living room"
    assert room["sensors"] == [
        {"temperature": "sensor.lounge", "last_seen": "sensor.lounge_last_seen"},
        {"temperature": "sensor.study"},
    ]
    assert "temperature_offset" not in room

    # Delete the room: its entity is removed
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get_entity_id("climate", DOMAIN, "heating_manager_downstairs_lounge")
    flow_id = await open_options(hass, entry)
    await menu(hass, flow_id, "zones")
    await form(hass, flow_id, {"zone": "downstairs"})
    await menu(hass, flow_id, "rooms")
    await form(hass, flow_id, {"room": "lounge"})
    result = await form(hass, flow_id, {"name": "x", "delete": True})
    assert result["step_id"] == "rooms"
    await save(hass, flow_id, "room")
    assert entry.options["zones"]["downstairs"]["rooms"] == {}
    assert ent_reg.async_get_entity_id("climate", DOMAIN, "heating_manager_downstairs_lounge") is None


async def test_delete_period_and_zone(hass, entry):
    flow_id = await open_options(hass, entry)
    await menu(hass, flow_id, "zones")
    await form(hass, flow_id, {"zone": "downstairs"})
    await menu(hass, flow_id, "schedule")
    await form(hass, flow_id, {"day": "weekend"})
    await form(hass, flow_id, {"period": "0"})
    result = await form(hass, flow_id, {"start": "06:30:00", "end": "22:00:00", "temperature": 19, "delete": True})
    assert result["step_id"] == "schedule_day"
    await form(hass, flow_id, {"period": "__back__"})
    await form(hass, flow_id, {"day": "__back__"})

    result = await menu(hass, flow_id, "zone_delete")
    result = await form(hass, flow_id, {"confirm": False})
    assert result["step_id"] == "zone"
    await menu(hass, flow_id, "zone_delete")
    result = await form(hass, flow_id, {"confirm": True})
    assert result["step_id"] == "zones"
    await form(hass, flow_id, {"zone": "__back__"})
    await save(hass, flow_id)

    assert entry.options["zones"] == {}
    assert hass.states.get("climate.downstairs") is None
    assert "zone_downstairs" not in device_ids(hass, entry)
    assert hass.states.get("climate.heating_manager") is not None


async def test_zone_demand_mode_back_to_global(hass, entry):
    options = base_options()
    options["zones"]["downstairs"]["heating_demand_mode"] = "zone_average"
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    flow_id = await open_options(hass, entry)
    await menu(hass, flow_id, "zones")
    await form(hass, flow_id, {"zone": "downstairs"})
    result = await menu(hass, flow_id, "zone_edit")
    result = await form(hass, flow_id, {"name": "Downstairs", "heating_demand_mode": "global"})
    assert result["step_id"] == "zone"
    await save(hass, flow_id)                                            # from the zone menu
    assert "heating_demand_mode" not in entry.options["zones"]["downstairs"]


async def test_reload_keeps_runtime_state(hass, entry):
    await entry.runtime_data.set_manual_room_temperature("downstairs", "lounge", 22.5)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("climate.downstairs_lounge").attributes["temperature"] == 22.5


async def test_learned_offsets_persist_across_reload(hass, entry, env):
    env["climate.lounge_trv"].set_internal_temperature(20.0)        # +3 vs the 17°C room
    await entry.runtime_data.async_refresh()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    learned = entry.runtime_data.trv_controller._get_ema_offset("downstairs", "lounge", "climate.lounge_trv")
    assert learned > 0.4


# ---------------------------------------------------------------------------
# entry_data helpers
# ---------------------------------------------------------------------------

def test_unique_id_for():
    assert unique_id_for("Living Room", {}, "room") == "living_room"
    assert unique_id_for("Living Room", {"living_room": {}}, "room") == "living_room_2"
    assert unique_id_for("!!!", set(), "room") == "room"


@pytest.mark.parametrize(
    ("start", "end", "size"),
    [("06:00", "08:00", 120), ("22:00", "06:00", 480), ("00:00", "00:00", 1440), ("21:00", "00:00", 180), ("bad", "x", 0)],
)
def test_period_minutes(start, end, size):
    assert len(period_minutes({"start": start, "end": end})) == size


def test_yaml_to_options_coerces_bad_settings():
    options = yaml_to_options({"minimum_temp": "warm", "heating_deadband": 99, "fallback_mode": "nope",
                               "analytics_enabled": "false", "zones": {"z": "bad", "y": {"rooms": {"r": "bad"}}}})
    settings = options["settings"]
    defaults = default_settings()
    assert settings["minimum_temp"] == defaults["minimum_temp"]
    assert settings["heating_deadband"] == defaults["heating_deadband"]
    assert settings["fallback_mode"] == defaults["fallback_mode"]
    assert settings["analytics_enabled"] is False
    assert list(options["zones"]) == ["y"]
    assert options["zones"]["y"]["rooms"] == {}


def test_options_to_runtime():
    config, settings = options_to_runtime(base_options(heating_demand_mode="zone_average", junk=1))
    assert config["heating_demand_mode"] == "zone_average"
    assert "downstairs" in config["zones"]
    assert "junk" not in settings
    assert settings["minimum_temp"] == default_settings()["minimum_temp"]


# ---------------------------------------------------------------------------
# UI text
# ---------------------------------------------------------------------------

async def test_every_ui_string_is_translated(hass, entry):
    """Each step, field, menu entry and select option used by the flows has English text."""
    import json
    from pathlib import Path

    from homeassistant.helpers.translation import async_get_translations

    from custom_components.heating_manager.entry_data import SETTINGS

    strings = json.loads(
        (Path(__file__).parents[1] / "custom_components/heating_manager/translations/en.json").read_text()
    )
    loaded = await async_get_translations(hass, "en", "options", [DOMAIN])
    assert loaded[f"component.{DOMAIN}.options.step.init.menu_options.save"] == "Save and close"

    steps = strings["options"]["step"]
    for spec in SETTINGS:
        step = "advanced" if spec.advanced else "settings"
        assert spec.key in steps[step]["data"], spec.key
        assert spec.key in steps[step]["data_description"], spec.key
        if spec.kind == "select":
            assert set(spec.options) <= set(strings["selector"][spec.key]["options"]), spec.key

    # Walk every screen of the options flow and check its fields are labelled
    seen: set[str] = set()

    async def check(result):
        step_id = result["step_id"]
        seen.add(step_id)
        assert "title" in steps[step_id], step_id
        if result["type"] is FlowResultType.MENU:
            for option in result["menu_options"]:
                assert option in steps[step_id]["menu_options"], (step_id, option)
        elif step_id != "room_last_seen":                       # fields are entity ids
            for key in result["data_schema"].schema:
                assert str(key) in steps[step_id].get("data", {}), (step_id, key)

    flow_id = await open_options(hass, entry)
    for step in ("settings", "advanced"):
        await check(await menu(hass, flow_id, step))
        hass.config_entries.options.async_abort(flow_id)
        flow_id = await open_options(hass, entry)
    await check(await menu(hass, flow_id, "zones"))
    await check(await form(hass, flow_id, {"zone": "downstairs"}))
    await check(await menu(hass, flow_id, "zone_edit"))
    await form(hass, flow_id, {"name": "Downstairs", "heating_demand_mode": "global"})
    await check(await menu(hass, flow_id, "zone_delete"))
    await form(hass, flow_id, {"confirm": False})
    await check(await menu(hass, flow_id, "rooms"))
    await check(await form(hass, flow_id, {"room": "lounge"}))
    await check(await form(hass, flow_id, {
        "name": "Lounge", "trvs": ["climate.lounge_trv"], "sensors": ["sensor.lounge"], "configure_last_seen": True,
    }))
    await form(hass, flow_id, {})
    await form(hass, flow_id, {"room": "__back__"})
    await check(await menu(hass, flow_id, "schedule"))
    await check(await form(hass, flow_id, {"day": "weekday"}))
    await check(await form(hass, flow_id, {"period": "0"}))
    hass.config_entries.options.async_abort(flow_id)
    assert seen | {"init", "save"} >= set(steps) - {"save"}

    config_steps = strings["config"]["step"]
    assert "name" in config_steps["user"]["data"]
    for key in ("zone_heating_demand_mode", "schedule_day", "fallback_mode", "heating_demand_mode"):
        assert key in strings["selector"]
    assert "yaml_imported" in strings["issues"]


async def test_room_named_zone_does_not_clash_with_zone_entity(hass, entry):
    flow_id = await open_options(hass, entry)
    await menu(hass, flow_id, "zones")
    await form(hass, flow_id, {"zone": "downstairs"})
    await menu(hass, flow_id, "rooms")
    await form(hass, flow_id, {"room": "__add__"})
    await form(hass, flow_id, {"name": "Zone", "trvs": ["climate.study_trv"]})
    await save(hass, flow_id, "room")
    assert "zone_2" in entry.options["zones"]["downstairs"]["rooms"]
    assert hass.states.get("climate.downstairs") is not None
    assert hass.states.get("climate.downstairs_zone") is not None
