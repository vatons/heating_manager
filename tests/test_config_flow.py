"""Tests for UI setup: config flow, zone/room subentries, settings, YAML import and migration."""
from __future__ import annotations

import os

import pytest

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData, section
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.heating_manager.const import DOMAIN
from custom_components.heating_manager.entry_data import (
    default_settings,
    entry_to_runtime,
    first_overlap,
    new_options,
    period_minutes,
    room_unique_id,
    unique_id_for,
    yaml_to_options,
    zone_unique_id,
)

from .conftest import FakeTRV, entry_from_options, local_dt, set_temp

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


async def setup_entry(hass, config_entry):
    if hass.config_entries.async_get_entry(config_entry.entry_id) is None:
        config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


@pytest.fixture
async def entry(hass, env):
    return await setup_entry(hass, entry_from_options(base_options()))


# -- Configure (options) helpers ---------------------------------------------

async def menu(hass, flow_id, step):
    return await hass.config_entries.options.async_configure(flow_id, {"next_step_id": step})


async def form(hass, flow_id, data):
    return await hass.config_entries.options.async_configure(flow_id, data)


async def open_options(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    return result["flow_id"]


# -- Subentry helpers --------------------------------------------------------

def subentries(entry, kind) -> dict[str, config_entries.ConfigSubentry]:
    """Subentries of a type keyed by zone_id / room_id."""
    key = "zone_id" if kind == "zone" else "room_id"
    return {s.data[key]: s for s in entry.subentries.values() if s.subentry_type == kind}


async def start_subentry(hass, entry, kind, subentry_id=None):
    context = {"source": "reconfigure", "subentry_id": subentry_id} if subentry_id else {"source": "user"}
    return await hass.config_entries.subentries.async_init((entry.entry_id, kind), context=context)


async def submit(hass, result, data):
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
    await hass.async_block_till_done()
    return result


def period(start, end, temperature):
    """A period as the time selector sends it."""
    return {"start": f"{start}:00", "end": f"{end}:00", "temperature": temperature}


def zone_input(name="Upstairs", weekday=(), weekend=None, mode="global", monitoring=False):
    return {
        "name": name,
        "heating_demand_mode": mode,
        "monitoring_only": monitoring,
        "weekday": {"periods": [period(*p) for p in weekday]},
        "weekend": (
            {"same_as_weekdays": True, "periods": []}
            if weekend is None
            else {"same_as_weekdays": False, "periods": [period(*p) for p in weekend]}
        ),
    }


def room_input(zone="downstairs", name="Study", trvs=("climate.study_trv",), sensors=(), offset=0, last_seen=None):
    return {
        "zone": zone,
        "name": name,
        "trvs": list(trvs),
        "sensors": list(sensors),
        "temperature_offset": offset,
        "advanced": {"last_seen": [
            {"temperature": temp, "last_seen": seen} for temp, seen in (last_seen or {}).items()
        ]},
    }


def shown(result) -> dict:
    """The values a form opens with (sections filled from their own defaults)."""
    data = {}
    for key, value in result["data_schema"].schema.items():
        if isinstance(value, section):
            data[str(key)] = value.schema({})
    return result["data_schema"](data)


def device_ids(hass, entry) -> set[str]:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {ident for device in devices for domain, ident in device.identifiers if domain == DOMAIN}


# ---------------------------------------------------------------------------
# Add Integration
# ---------------------------------------------------------------------------

async def test_user_flow_creates_entry_with_first_zone(hass, env):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "  "})
    assert result["errors"] == {"name": "name_required"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Upstairs"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    entry = result["result"]
    assert entry.version == 2
    assert entry.options == {"settings": default_settings()}
    zone = subentries(entry, "zone")["upstairs"]
    assert zone.title == "Upstairs · no rooms"
    assert zone.data["schedule"]["weekday"] == [{"start": "06:30", "end": "22:00", "temperature": 19.0}]
    assert zone.data["weekend_same_as_weekday"] is True
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
# Zones
# ---------------------------------------------------------------------------

async def test_add_zone(hass, entry):
    result = await start_subentry(hass, entry, "zone")
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    keys = {str(k) for k in result["data_schema"].schema}
    assert keys == {"name", "heating_demand_mode", "monitoring_only", "weekday", "weekend"}

    result = await submit(hass, result, zone_input(
        "Upstairs", weekday=[("17:00", "22:30", 21), ("06:00", "08:00", 20)],
        weekend=[("08:00", "23:00", 20.5)], mode="zone_average",
    ))
    assert result["type"] is FlowResultType.CREATE_ENTRY

    zone = subentries(entry, "zone")["upstairs"]
    assert zone.title == "Upstairs · no rooms"
    assert zone.data == {
        "zone_id": "upstairs",
        "name": "Upstairs",
        "schedule": {
            "weekday": [                                              # sorted, seconds dropped
                {"start": "06:00", "end": "08:00", "temperature": 20.0},
                {"start": "17:00", "end": "22:30", "temperature": 21.0},
            ],
            "weekend": [{"start": "08:00", "end": "23:00", "temperature": 20.5}],
        },
        "weekend_same_as_weekday": False,
        "heating_demand_mode": "zone_average",
    }
    # Applied straight away
    assert hass.states.get("climate.upstairs") is not None
    assert "zone_upstairs" in device_ids(hass, entry)


async def test_weekend_same_as_weekdays(hass, entry):
    result = await start_subentry(hass, entry, "zone")
    await submit(hass, result, zone_input("Upstairs", weekday=[("06:00", "08:00", 20)]))
    zone = subentries(entry, "zone")["upstairs"]
    assert zone.data["weekend_same_as_weekday"] is True
    runtime = entry_to_runtime(entry)[0]["zones"]["upstairs"]
    assert runtime["schedule"]["weekend"] == runtime["schedule"]["weekday"]


@pytest.mark.parametrize(
    ("weekday", "weekend", "message"),
    [
        ([("06:00", "08:00", 20), ("07:30", "09:00", 19)], None,
         "Weekdays: 06:00–08:00 and 07:30–09:00"),
        ([("22:00", "02:00", 18), ("01:00", "05:00", 16)], None,                 # past midnight
         "Weekdays: 01:00–05:00 and 22:00–02:00"),
        ([("00:00", "00:00", 18), ("12:00", "13:00", 21)], None,                 # all day
         "Weekdays: 00:00–00:00 and 12:00–13:00"),
        ([("06:00", "08:00", 20)], [("09:00", "12:00", 20), ("11:00", "14:00", 19)],
         "Weekends: 09:00–12:00 and 11:00–14:00"),
    ],
)
async def test_overlapping_periods_are_rejected(hass, entry, weekday, weekend, message):
    result = await start_subentry(hass, entry, "zone")
    result = await submit(hass, result, zone_input("Upstairs", weekday=weekday, weekend=weekend))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "overlap"}
    assert result["description_placeholders"]["overlap"] == message
    assert "upstairs" not in subentries(entry, "zone")
    # What was entered is shown again
    values = shown(result)
    assert values["name"] == "Upstairs"
    assert sorted(p["start"] for p in values["weekday"]["periods"]) == sorted(f"{p[0]}:00" for p in weekday)
    assert values["weekend"]["same_as_weekdays"] is (weekend is None)


async def test_periods_that_only_touch_are_allowed(hass, entry):
    result = await start_subentry(hass, entry, "zone")
    result = await submit(hass, result, zone_input(
        "Upstairs", weekday=[("06:00", "08:00", 20), ("08:00", "09:00", 19), ("22:00", "06:00", 16)],
    ))
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_weekend_periods_ignored_when_same_as_weekdays(hass, entry):
    data = zone_input("Upstairs", weekday=[("06:00", "08:00", 20)])
    data["weekend"]["periods"] = [period("09:00", "12:00", 20), period("11:00", "14:00", 19)]
    result = await submit(hass, await start_subentry(hass, entry, "zone"), data)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_zone_validation_errors(hass, entry):
    result = await start_subentry(hass, entry, "zone")
    result = await submit(hass, result, zone_input("  "))
    assert result["errors"] == {"name": "name_required"}
    result = await submit(hass, result, zone_input("downstairs"))
    assert result["errors"] == {"name": "name_in_use"}
    bad = zone_input("Upstairs")
    bad["weekday"]["periods"] = [{"start": "25:00:00", "end": "08:00:00", "temperature": 20}]
    with pytest.raises(InvalidData):                                    # the time selector rejects it
        await submit(hass, result, bad)
    result = await submit(hass, result, zone_input("Upstairs"))
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure_zone(hass, entry):
    zone = subentries(entry, "zone")["downstairs"]
    result = await start_subentry(hass, entry, "zone", zone.subentry_id)
    assert result["step_id"] == "reconfigure"
    # The form shows the current values
    values = shown(result)
    assert values["name"] == "Downstairs"
    assert values["weekday"]["periods"] == [period("06:30", "22:00", 19.0)]

    result = await submit(hass, result, zone_input(
        "Ground floor", weekday=[("07:00", "09:00", 20)], monitoring=True,
    ))
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    zone = subentries(entry, "zone")["downstairs"]                      # id kept on rename
    assert zone.title == "Ground floor · 1 room · monitoring only"
    assert zone.data["monitoring_only"] is True
    assert zone.data["schedule"]["weekday"] == [{"start": "07:00", "end": "09:00", "temperature": 20.0}]
    # Rooms show their zone's new name
    assert subentries(entry, "room")["lounge"].title == "Ground floor › Lounge · 1 TRV · 1 sensor"
    # Entity ids are unchanged
    assert hass.states.get("climate.downstairs").attributes["monitoring_only"] is True


async def test_reconfigure_zone_rejects_overlap(hass, entry):
    zone = subentries(entry, "zone")["downstairs"]
    result = await start_subentry(hass, entry, "zone", zone.subentry_id)
    result = await submit(hass, result, zone_input("Downstairs", weekday=[("06:00", "09:00", 20), ("08:00", "10:00", 18)]))
    assert result["errors"] == {"base": "overlap"}
    assert subentries(entry, "zone")["downstairs"].data["schedule"]["weekday"][0]["start"] == "06:30"


async def test_zone_demand_mode_back_to_global(hass, env):
    options = base_options()
    options["zones"]["downstairs"]["heating_demand_mode"] = "zone_average"
    entry = await setup_entry(hass, entry_from_options(options))
    zone = subentries(entry, "zone")["downstairs"]
    assert zone.data["heating_demand_mode"] == "zone_average"
    result = await start_subentry(hass, entry, "zone", zone.subentry_id)
    assert shown(result)["heating_demand_mode"] == "zone_average"
    await submit(hass, result, zone_input("Downstairs", weekday=[("06:30", "22:00", 19)]))
    assert "heating_demand_mode" not in subentries(entry, "zone")["downstairs"].data


async def test_delete_zone_deletes_its_rooms(hass, entry):
    zone = subentries(entry, "zone")["downstairs"]
    assert hass.config_entries.async_remove_subentry(entry, zone.subentry_id)
    await hass.async_block_till_done()
    assert subentries(entry, "zone") == {}
    assert subentries(entry, "room") == {}
    assert hass.states.get("climate.downstairs") is None
    assert hass.states.get("climate.downstairs_lounge") is None
    assert "zone_downstairs" not in device_ids(hass, entry)
    assert hass.states.get("climate.heating_manager") is not None


# ---------------------------------------------------------------------------
# Monitoring-only zones
# ---------------------------------------------------------------------------

async def test_toggle_monitoring_only(hass, entry, env):
    async def set_monitoring(value):
        zone = subentries(entry, "zone")["downstairs"]
        result = await start_subentry(hass, entry, "zone", zone.subentry_id)
        await submit(hass, result, zone_input("Downstairs", weekday=[("00:00", "00:00", 19)], monitoring=value))

    await set_monitoring(False)
    # Cold lounge: normally heats
    assert hass.states.get("climate.downstairs").attributes["hvac_action"] == "heating"

    await set_monitoring(True)
    assert subentries(entry, "zone")["downstairs"].data["monitoring_only"] is True
    zone = hass.states.get("climate.downstairs")
    assert zone.attributes["hvac_action"] == "idle"
    assert zone.attributes["monitoring_only"] is True
    lounge = hass.states.get("climate.downstairs_lounge")
    assert lounge.attributes["hvac_action"] == "idle"
    assert lounge.attributes["current_temperature"] == 17.0
    glob = hass.states.get("climate.heating_manager")
    assert glob.attributes["hvac_action"] == "idle"
    assert glob.attributes["current_temperature"] is None      # only zone is monitoring-only

    await set_monitoring(False)
    assert "monitoring_only" not in subentries(entry, "zone")["downstairs"].data
    assert hass.states.get("climate.downstairs").attributes["hvac_action"] == "heating"


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

async def test_add_room(hass, entry):
    result = await start_subentry(hass, entry, "room")
    assert result["step_id"] == "user"
    keys = {str(k) for k in result["data_schema"].schema}
    assert keys == {"zone", "name", "trvs", "sensors", "temperature_offset", "advanced"}

    result = await submit(hass, result, room_input(trvs=(), sensors=()))
    assert result["errors"] == {"base": "room_empty"}
    result = await submit(hass, result, room_input(name=""))
    assert result["errors"] == {"name": "name_required"}
    result = await submit(hass, result, room_input(
        sensors=["sensor.study"], offset=-1.5, last_seen={"sensor.study": "sensor.study_last_seen"},
    ))
    assert result["type"] is FlowResultType.CREATE_ENTRY

    room = subentries(entry, "room")["study"]
    assert room.title == "Downstairs › Study · 1 TRV · 1 sensor"
    assert subentries(entry, "zone")["downstairs"].title == "Downstairs · 2 rooms"
    assert room.data == {
        "room_id": "study",
        "zone_id": "downstairs",
        "name": "Study",
        "trvs": ["climate.study_trv"],
        "sensors": [{"temperature": "sensor.study", "last_seen": "sensor.study_last_seen"}],
        "temperature_offset": -1.5,
    }
    study = hass.states.get("climate.downstairs_study")
    assert study is not None
    # 12:00 is inside the 06:30-22:00 period at 19°C; offset -1.5
    assert study.attributes["temperature"] == 17.5


async def test_room_needs_a_zone(hass, env):
    entry = await setup_entry(hass, entry_from_options(new_options()))
    result = await start_subentry(hass, entry, "room")
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_zones"


async def test_room_trv_used_by_another_room(hass, entry):
    result = await start_subentry(hass, entry, "room")
    result = await submit(hass, result, room_input(trvs=["climate.lounge_trv", "climate.study_trv"]))
    assert result["errors"] == {"trvs": "trv_in_use"}
    assert result["description_placeholders"]["trv_conflict"] == "climate.lounge_trv (Downstairs › Lounge)"
    result = await submit(hass, result, room_input(trvs=["climate.study_trv"]))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The room that already has the TRV can keep it
    lounge = subentries(entry, "room")["lounge"]
    result = await start_subentry(hass, entry, "room", lounge.subentry_id)
    result = await submit(hass, result, room_input(name="Lounge", trvs=["climate.lounge_trv"], sensors=["sensor.lounge"]))
    assert result["reason"] == "reconfigure_successful"


async def test_reconfigure_room(hass, entry):
    lounge = subentries(entry, "room")["lounge"]
    result = await start_subentry(hass, entry, "room", lounge.subentry_id)
    assert result["step_id"] == "reconfigure"
    values = shown(result)
    assert values["sensors"] == ["sensor.lounge"]
    assert values["advanced"]["last_seen"] == [{"temperature": "sensor.lounge", "last_seen": "sensor.lounge_last_seen"}]

    result = await submit(hass, result, room_input(
        name="Living room", trvs=["climate.lounge_trv"], sensors=["sensor.lounge", "sensor.study"],
        last_seen={"sensor.lounge": "sensor.lounge_last_seen"},
    ))
    assert result["reason"] == "reconfigure_successful"
    room = subentries(entry, "room")["lounge"]                       # id unchanged on rename
    assert room.title == "Downstairs › Living room · 1 TRV · 2 sensors"
    assert room.data["sensors"] == [
        {"temperature": "sensor.lounge", "last_seen": "sensor.lounge_last_seen"},
        {"temperature": "sensor.study"},
    ]
    assert "temperature_offset" not in room.data
    assert hass.states.get("climate.downstairs_lounge") is not None


async def test_move_room_to_another_zone_keeps_entity(hass, entry):
    await submit(hass, await start_subentry(hass, entry, "zone"), zone_input("Upstairs", weekday=[("06:00", "08:00", 20)]))
    lounge = subentries(entry, "room")["lounge"]
    result = await start_subentry(hass, entry, "room", lounge.subentry_id)
    await submit(hass, result, room_input(zone="upstairs", name="Lounge", trvs=["climate.lounge_trv"]))
    assert subentries(entry, "room")["lounge"].title == "Upstairs › Lounge · 1 TRV"
    zones = subentries(entry, "zone")
    assert (zones["downstairs"].title, zones["upstairs"].title) == ("Downstairs · no rooms", "Upstairs · 1 room")
    assert entry_to_runtime(entry)[0]["zones"]["upstairs"]["rooms"].keys() == {"lounge"}
    entity_id = er.async_get(hass).async_get_entity_id("climate", DOMAIN, room_unique_id("lounge"))
    assert entity_id == "climate.downstairs_lounge"
    assert hass.states.get(entity_id) is not None


async def test_delete_room(hass, entry):
    ent_reg = er.async_get(hass)
    assert ent_reg.async_get_entity_id("climate", DOMAIN, room_unique_id("lounge"))
    lounge = subentries(entry, "room")["lounge"]
    assert hass.config_entries.async_remove_subentry(entry, lounge.subentry_id)
    await hass.async_block_till_done()
    assert subentries(entry, "room") == {}
    assert ent_reg.async_get_entity_id("climate", DOMAIN, room_unique_id("lounge")) is None
    assert hass.states.get("climate.downstairs") is not None


async def test_room_ids_unique_across_zones(hass, entry):
    await submit(hass, await start_subentry(hass, entry, "zone"), zone_input("Upstairs", weekday=[("06:00", "08:00", 20)]))
    await submit(hass, await start_subentry(hass, entry, "room"), room_input(zone="upstairs", name="Lounge"))
    assert set(subentries(entry, "room")) == {"lounge", "lounge_2"}
    assert hass.states.get("climate.upstairs_lounge") is not None
    assert hass.states.get("climate.downstairs_lounge") is not None


async def test_room_named_zone_does_not_clash_with_zone_entity(hass, entry):
    await submit(hass, await start_subentry(hass, entry, "room"), room_input(name="Zone"))
    assert "zone" in subentries(entry, "room")
    assert hass.states.get("climate.downstairs") is not None
    assert hass.states.get("climate.downstairs_zone") is not None


async def test_titles_list_rooms_under_their_zone(hass, env):
    """Titles from 3.0.0 ("Lounge (Downstairs)") are updated; sorted, rooms follow their zone."""
    options = base_options()
    options["zones"]["upstairs"] = {"name": "Upstairs", "monitoring_only": True, "schedule": {}, "rooms": {
        "bathroom": {"name": "Bathroom", "trvs": [], "sensors": [{"temperature": "sensor.study"}]}}}
    options["zones"]["downstairs"]["rooms"]["study"] = {
        "name": "Study", "trvs": ["climate.study_trv"], "sensors": [{"temperature": "sensor.study"}]}
    config_entry = entry_from_options(options)
    for sub in list(config_entry.subentries.values()):
        if sub.subentry_type == "room":
            object.__setattr__(sub, "title", f"{sub.data['name']} (old)")
    await setup_entry(hass, config_entry)
    titles = sorted((s.title for s in config_entry.subentries.values()), key=str.casefold)
    assert titles == [
        "Downstairs · 2 rooms",
        "Downstairs › Lounge · 1 TRV · 1 sensor",
        "Downstairs › Study · 1 TRV · 1 sensor",
        "Upstairs · 1 room · monitoring only",
        "Upstairs › Bathroom · 1 sensor",
    ]


async def test_title_only_change_does_not_reload(hass, entry):
    coordinator = entry.runtime_data
    zone = subentries(entry, "zone")["downstairs"]
    hass.config_entries.async_update_subentry(entry, zone, title="Something else")
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator                          # not reloaded
    assert subentries(entry, "zone")["downstairs"].title == "Downstairs · 1 room"


async def test_orphan_rooms_removed_on_setup(hass, env):
    """A room whose zone is gone (e.g. HA stopped between the two deletions) is removed."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={}, version=2, options={"settings": {}},
        subentries_data=[{
            "subentry_type": "room", "title": "Lounge (Gone)", "unique_id": "lounge",
            "data": {"room_id": "lounge", "zone_id": "gone", "name": "Lounge", "trvs": [], "sensors": []},
        }],
    )
    await setup_entry(hass, config_entry)
    assert subentries(config_entry, "room") == {}


# ---------------------------------------------------------------------------
# Configure: settings
# ---------------------------------------------------------------------------

async def test_settings_form(hass, entry):
    flow_id = await open_options(hass, entry)
    result = await menu(hass, flow_id, "settings")
    assert result["step_id"] == "settings"
    sections = {str(k): v for k, v in result["data_schema"].schema.items()}
    assert set(sections) == {
        "temperatures", "heating_demand", "boiler", "sensors", "boost", "trv_control", "analytics", "updates",
    }
    assert sections["trv_control"].options["collapsed"] is True
    assert sections["temperatures"].options["collapsed"] is False

    current = shown(result)
    current["temperatures"]["minimum_temp"] = 14
    current["sensors"]["fallback_mode"] = "trv"
    current["updates"]["update_interval"] = 120
    current["boiler"].update(min_boiler_on_time=5, min_boiler_off_time=3)
    result = await form(hass, flow_id, current)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    settings = entry.options["settings"]
    assert settings["minimum_temp"] == 14 and settings["fallback_mode"] == "trv"
    assert settings["update_interval"] == 120
    assert set(settings) == set(default_settings())
    # The entry reloaded with the new settings; zones and rooms untouched
    coordinator = entry.runtime_data
    assert coordinator.minimum_temp == 14
    assert coordinator.update_interval.total_seconds() == 120
    assert coordinator.temperature_manager.fallback_mode == "trv"
    assert coordinator.min_boiler_on_time.total_seconds() == 300
    assert coordinator.min_boiler_off_time.total_seconds() == 180
    assert set(subentries(entry, "room")) == {"lounge"}
    assert "demand_hold" in hass.states.get("climate.heating_manager").attributes


async def test_closing_settings_without_submit_changes_nothing(hass, entry):
    flow_id = await open_options(hass, entry)
    await menu(hass, flow_id, "settings")
    hass.config_entries.options.async_abort(flow_id)
    assert entry.options["settings"]["minimum_temp"] == default_settings()["minimum_temp"]


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
# YAML in configuration.yaml (imported once)
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


async def test_yaml_import_creates_entry_with_zones_and_rooms(hass, env, tmp_path):
    ok, path = await setup_yaml(hass, tmp_path, boost_duration=45)
    assert ok
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.version == 2

    settings = entry.options["settings"]
    assert settings["minimum_temp"] == 12
    assert settings["heating_demand_mode"] == "zone_average"
    assert settings["boost_duration"] == 45                       # from configuration.yaml

    zone = subentries(entry, "zone")["downstairs"]
    assert zone.title == "Downstairs · 2 rooms"
    assert zone.data["heating_demand_mode"] == "any_room"
    assert zone.data["schedule"]["weekday"] == [                  # sorted, normalised, invalid dropped
        {"start": "06:30", "end": "08:00", "temperature": 19.5},
        {"start": "16:00", "end": "21:00", "temperature": 20.0},
    ]
    assert zone.data["schedule"]["weekend"] == [{"start": "07:00", "end": "00:00", "temperature": 20.0}]
    assert zone.data["weekend_same_as_weekday"] is False

    rooms = subentries(entry, "room")
    assert rooms["lounge"].title == "Downstairs › Lounge · 1 TRV · 1 sensor"
    assert rooms["lounge"].data["temperature_offset"] == -1.0
    assert rooms["lounge"].data["sensors"] == [
        {"temperature": "sensor.lounge", "last_seen": "sensor.lounge_last_seen"}
    ]
    assert rooms["study"].data == {
        "room_id": "study", "zone_id": "downstairs", "name": "study", "previous_zone_id": "downstairs",
        "trvs": ["climate.study_trv"], "sensors": [{"temperature": "sensor.study"}],
    }
    assert hass.states.get("climate.downstairs_lounge") is not None

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "yaml_imported")
    assert issue is not None
    assert issue.translation_placeholders == {"config_file": path}


async def test_yaml_ignored_once_imported(hass, env, tmp_path):
    existing = entry_from_options(base_options(minimum_temp=15))
    existing.add_to_hass(hass)
    ok, _ = await setup_yaml(hass, tmp_path)
    assert ok
    assert hass.config_entries.async_entries(DOMAIN) == [existing]
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


async def test_import_monitoring_only_from_yaml(hass, env, tmp_path):
    text = LEGACY_YAML.replace("    heating_demand_mode: any_room\n", "    monitoring_only: true\n")
    await setup_yaml(hass, tmp_path, text=text)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert subentries(entry, "zone")["downstairs"].data["monitoring_only"] is True


async def test_yaml_with_overlapping_periods_imports_and_warns(hass, env, tmp_path, caplog):
    text = LEGACY_YAML.replace("{start: 16:00, end: 21:00", "{start: 07:00, end: 21:00")
    await setup_yaml(hass, tmp_path, text=text)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert len(subentries(entry, "zone")["downstairs"].data["schedule"]["weekday"]) == 2
    assert "Downstairs weekdays: 06:30–08:00 and 07:00–21:00" in caplog.text
    # Editing the zone asks for the overlap to be fixed
    zone = subentries(entry, "zone")["downstairs"]
    result = await start_subentry(hass, entry, "zone", zone.subentry_id)
    result = await submit(hass, result, shown(result))
    assert result["errors"] == {"base": "overlap"}


async def test_yaml_entities_keep_entity_ids_and_state(hass, hass_storage, env, tmp_path):
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
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    registered = ent_reg.async_get("climate.lounge_hm")
    assert registered.config_entry_id == entry.entry_id
    assert registered.config_subentry_id == subentries(entry, "room")["lounge"].subentry_id


# ---------------------------------------------------------------------------
# Upgrading from 2.x (zones and rooms in options)
# ---------------------------------------------------------------------------

async def test_migrate_v1_entry_to_subentries(hass, env):
    options = base_options(minimum_temp=14)
    options["zones"]["downstairs"]["heating_demand_mode"] = "any_room"
    options["zones"]["upstairs"] = {
        "name": "Upstairs", "monitoring_only": True,
        "schedule": {"weekday": [{"start": "06:00", "end": "08:00", "temperature": 20.0}], "weekend": []},
        "rooms": {"lounge": {"name": "Lounge", "trvs": ["climate.study_trv"], "sensors": []}},
    }
    entry = MockConfigEntry(domain=DOMAIN, title="Heating Manager", data={}, options=options, version=1)
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    for unique_id, object_id in [
        ("heating_manager:room:downstairs:lounge", "my_lounge"),         # 2.2 ids
        ("heating_manager:room:upstairs:lounge", "my_upstairs_lounge"),
        ("heating_manager:zone:downstairs", "my_downstairs"),
        ("heating_manager:global", "my_heating"),
    ]:
        ent_reg.async_get_or_create("climate", DOMAIN, unique_id, suggested_object_id=object_id, config_entry=entry)
    await setup_entry(hass, entry)

    assert entry.version == 2
    assert entry.options == {"settings": options["settings"]}
    zones = subentries(entry, "zone")
    assert set(zones) == {"downstairs", "upstairs"}
    assert zones["downstairs"].data["heating_demand_mode"] == "any_room"
    assert zones["upstairs"].data["monitoring_only"] is True
    assert zones["upstairs"].data["weekend_same_as_weekday"] is False
    rooms = subentries(entry, "room")
    assert set(rooms) == {"lounge", "upstairs_lounge"}                # room ids are unique now
    assert rooms["upstairs_lounge"].title == "Upstairs › Lounge · 1 TRV"
    assert entry.runtime_data.minimum_temp == 14

    for entity_id, unique_id, subentry in [
        ("climate.my_lounge", room_unique_id("lounge"), rooms["lounge"]),
        ("climate.my_upstairs_lounge", room_unique_id("upstairs_lounge"), rooms["upstairs_lounge"]),
        ("climate.my_downstairs", zone_unique_id("downstairs"), zones["downstairs"]),
        ("climate.my_heating", "heating_manager:global", None),
    ]:
        registered = ent_reg.async_get(entity_id)
        assert registered.unique_id == unique_id
        assert registered.config_subentry_id == (subentry.subentry_id if subentry else None)
        assert hass.states.get(entity_id) is not None

    # A renamed-on-migration room keeps its entity id when edited later
    result = await start_subentry(hass, entry, "room", rooms["upstairs_lounge"].subentry_id)
    await submit(hass, result, room_input(zone="upstairs", name="Lounge", trvs=["climate.study_trv"]))
    assert subentries(entry, "room")["upstairs_lounge"].data["previous_room_id"] == "lounge"
    assert hass.states.get("climate.my_upstairs_lounge") is not None


async def test_migrate_v1_entry_with_21_unique_ids(hass, env):
    """Entities from 2.0/2.1 (old unique ids, owned by the entry) keep their entity ids."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=base_options(), version=1)
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    for old, object_id in [
        ("heating_manager_downstairs_lounge", "my_lounge"),
        ("heating_manager_downstairs_zone", "my_downstairs"),
        ("heating_manager_global", "my_heating"),
    ]:
        ent_reg.async_get_or_create("climate", DOMAIN, old, suggested_object_id=object_id, config_entry=entry)
    await setup_entry(hass, entry)

    for entity_id, unique_id in [
        ("climate.my_lounge", "heating_manager:room:lounge"),
        ("climate.my_downstairs", "heating_manager:zone:downstairs"),
        ("climate.my_heating", "heating_manager:global"),
    ]:
        assert ent_reg.async_get(entity_id).unique_id == unique_id
        assert hass.states.get(entity_id) is not None
    assert not [e for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
                if not e.unique_id.startswith("heating_manager:")]


def two_zone_options():
    options = new_options()
    options["zones"]["up"] = {"name": "Up", "schedule": {}, "rooms": {
        "stairs_bed": {"name": "Stairs Bed", "trvs": ["climate.lounge_trv"], "sensors": []}}}
    options["zones"]["up_stairs"] = {"name": "Up Stairs", "schedule": {}, "rooms": {
        "bed": {"name": "Bed", "trvs": ["climate.study_trv"], "sensors": []}}}
    return options


async def test_zone_and_room_ids_that_used_to_collide_get_separate_entities(hass, env):
    """Zone 'up' + room 'stairs_bed' and zone 'up_stairs' + room 'bed' were one 2.1 unique id."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=two_zone_options(), version=1)
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "climate", DOMAIN, "heating_manager_up_stairs_bed", suggested_object_id="kept", config_entry=entry
    )
    await setup_entry(hass, entry)
    assert ent_reg.async_get("climate.kept").unique_id == room_unique_id("stairs_bed")
    other = ent_reg.async_get_entity_id("climate", DOMAIN, room_unique_id("bed"))
    assert other and hass.states.get(other) is not None


async def test_newer_entry_version_is_refused(hass, env):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={}, version=3)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.MIGRATION_ERROR
    await hass.config_entries.async_remove(entry.entry_id)


# ---------------------------------------------------------------------------
# Configure: Import from YAML file
# ---------------------------------------------------------------------------

@pytest.fixture
def config_file(hass):
    """Write files into HA's config dir; removed afterwards."""
    created = []

    def _write(name, text):
        path = hass.config.path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        created.append(path)
        return path

    yield _write
    for path in created:
        os.remove(path)


async def start_import(hass, entry, path, import_settings=True):
    flow_id = await open_options(hass, entry)
    result = await menu(hass, flow_id, "import_yaml")
    assert result["step_id"] == "import_yaml"
    result = await form(hass, flow_id, {"path": path, "import_settings": import_settings})
    return flow_id, result


async def confirm_import(hass, flow_id):
    result = await form(hass, flow_id, {"confirm": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result


async def test_import_yaml_into_empty_entry(hass, env, config_file):
    empty = await setup_entry(hass, entry_from_options(new_options()))
    assert empty.subentries == {}

    config_file("hm_import_test.yaml", LEGACY_YAML)
    flow_id, result = await start_import(hass, empty, "hm_import_test.yaml")
    assert result["step_id"] == "import_yaml_confirm"
    zones_text = result["description_placeholders"]["zones"]
    assert "Downstairs: 2 room(s), 3 schedule period(s)" in zones_text
    assert "replaces" not in zones_text
    assert result["description_placeholders"]["settings"] == "2"     # minimum_temp, heating_demand_mode
    assert result["description_placeholders"]["warnings"] == ""

    await confirm_import(hass, flow_id)
    assert set(subentries(empty, "zone")) == {"downstairs"}
    assert set(subentries(empty, "room")) == {"lounge", "study"}
    settings = empty.options["settings"]
    assert settings["minimum_temp"] == 12
    assert settings["heating_demand_mode"] == "zone_average"
    assert settings["boost_duration"] == default_settings()["boost_duration"]
    assert hass.states.get("climate.downstairs_lounge") is not None
    assert hass.states.get("climate.downstairs_study") is not None


async def test_import_yaml_replaces_zone_and_keeps_others(hass, entry, config_file):
    await submit(hass, await start_subentry(hass, entry, "zone"), zone_input("Upstairs", weekday=[("06:00", "08:00", 20)]))
    hass.config_entries.async_update_entry(entry, options={"settings": {**default_settings(), "minimum_temp": 14}})
    await hass.async_block_till_done()

    config_file("hm_import_test.yaml", LEGACY_YAML)
    flow_id, result = await start_import(hass, entry, "hm_import_test.yaml", import_settings=False)
    assert "(replaces the existing zone)" in result["description_placeholders"]["zones"]
    assert result["description_placeholders"]["settings"] == "0"
    await confirm_import(hass, flow_id)

    assert set(subentries(entry, "zone")) == {"downstairs", "upstairs"}   # UI-only zone kept
    rooms = subentries(entry, "room")
    assert set(rooms) == {"lounge", "study"}                              # replaced from file
    assert rooms["lounge"].data["temperature_offset"] == -1.0
    assert len(subentries(entry, "zone")["downstairs"].data["schedule"]["weekday"]) == 2
    assert entry.options["settings"]["minimum_temp"] == 14                # settings untouched
    assert hass.states.get("climate.downstairs_lounge") is not None
    assert hass.states.get("climate.upstairs") is not None


async def test_import_yaml_room_id_clash_with_other_zone(hass, entry, config_file):
    config_file("hm_import_test.yaml", LEGACY_YAML.replace("downstairs:", "upstairs:", 1).replace(
        "name: Downstairs", "name: Upstairs").replace("climate.lounge_trv", "climate.other_trv"))
    flow_id, _ = await start_import(hass, entry, "hm_import_test.yaml")
    await confirm_import(hass, flow_id)
    rooms = subentries(entry, "room")
    assert set(rooms) == {"lounge", "upstairs_lounge", "study"}
    assert rooms["upstairs_lounge"].data["zone_id"] == "upstairs"


async def test_import_yaml_declined_changes_nothing(hass, entry, config_file):
    config_file("hm_import_test.yaml", LEGACY_YAML.replace("downstairs:", "garage:", 1))
    flow_id, result = await start_import(hass, entry, "hm_import_test.yaml")
    result = await form(hass, flow_id, {"confirm": False})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "init"
    hass.config_entries.options.async_abort(flow_id)
    assert set(subentries(entry, "zone")) == {"downstairs"}


async def test_import_yaml_warns_about_overlaps(hass, entry, config_file):
    config_file("hm_import_test.yaml", LEGACY_YAML.replace("{start: 16:00, end: 21:00", "{start: 07:00, end: 21:00"))
    flow_id, result = await start_import(hass, entry, "hm_import_test.yaml")
    warnings = result["description_placeholders"]["warnings"]
    assert "Schedule periods that overlap" in warnings
    assert "- Downstairs weekdays: 06:30–08:00 and 07:00–21:00" in warnings
    hass.config_entries.options.async_abort(flow_id)


async def test_import_yaml_in_subfolder_with_absolute_path(hass, entry, config_file):
    path = config_file("packages/hm_import_test.yml", LEGACY_YAML)
    flow_id, result = await start_import(hass, entry, path)
    assert result["step_id"] == "import_yaml_confirm"
    hass.config_entries.options.async_abort(flow_id)


@pytest.mark.parametrize(
    ("path", "text", "error"),
    [
        ("hm_missing.yaml", None, "file_not_found"),
        ("hm_bad.yaml", "zones: [unclosed", "invalid_yaml"),
        ("hm_list.yaml", "- a\n- b\n", "invalid_yaml"),
        ("hm_empty_zones.txt", "zones: {}", "no_zones"),
        ("../outside.yaml", None, "path_outside_config"),
        ("/etc/hostname.yaml", None, "path_outside_config"),
        ("hm_nozones.yaml", "minimum_temp: 10\nrooms: {}\n", "no_zones"),
    ],
)
async def test_import_yaml_errors(hass, entry, config_file, path, text, error):
    if text is not None:
        config_file(path, text)
    flow_id, result = await start_import(hass, entry, path)
    assert result["step_id"] == "import_yaml"
    assert result["errors"] == {"base": error}
    if path == "hm_nozones.yaml":
        assert result["description_placeholders"]["keys"] == "minimum_temp, rooms"
    hass.config_entries.options.async_abort(flow_id)


async def test_import_yaml_from_backup_file_name(hass, entry, config_file):
    """Any file name works, e.g. a dated backup of heating_manager.yaml."""
    config_file("heating_manager.yaml.20261008", LEGACY_YAML)
    flow_id, result = await start_import(hass, entry, "heating_manager.yaml.20261008")
    assert result["step_id"] == "import_yaml_confirm"
    assert "Downstairs: 2 room(s)" in result["description_placeholders"]["zones"]
    hass.config_entries.options.async_abort(flow_id)


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


def test_first_overlap():
    p = lambda s, e: {"start": s, "end": e, "temperature": 20}  # noqa: E731
    assert first_overlap([p("06:00", "08:00"), p("08:00", "10:00")]) is None
    assert first_overlap([p("06:00", "08:00"), p("07:59", "10:00")]) == "06:00–08:00 and 07:59–10:00"
    assert first_overlap([p("23:00", "01:00"), p("00:30", "02:00")]) == "23:00–01:00 and 00:30–02:00"
    assert first_overlap([p("21:00", "00:00"), p("00:00", "06:00")]) is None
    assert first_overlap([]) is None


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


async def test_entry_to_runtime(hass, env):
    config_entry = entry_from_options(base_options(heating_demand_mode="zone_average", junk=1))
    config, settings = entry_to_runtime(config_entry)
    assert config["heating_demand_mode"] == "zone_average"
    assert set(config["zones"]) == {"downstairs"}
    assert set(config["zones"]["downstairs"]["rooms"]) == {"lounge"}
    assert "junk" not in settings
    assert settings["minimum_temp"] == default_settings()["minimum_temp"]


# ---------------------------------------------------------------------------
# UI text
# ---------------------------------------------------------------------------

async def test_every_ui_string_is_translated(hass, entry, config_file):
    """Each step, section, field and select option used by the flows has English text."""
    import json
    from pathlib import Path

    from homeassistant.helpers.translation import async_get_translations

    from custom_components.heating_manager.entry_data import SETTINGS

    strings = json.loads(
        (Path(__file__).parents[1] / "custom_components/heating_manager/translations/en.json").read_text()
    )
    loaded = await async_get_translations(hass, "en", "config_subentries", [DOMAIN])
    assert loaded[f"component.{DOMAIN}.config_subentries.zone.initiate_flow.user"] == "Add zone"
    assert loaded[f"component.{DOMAIN}.config_subentries.room.initiate_flow.user"] == "Add room"

    def check_form(step_strings, result):
        assert "title" in step_strings, result["step_id"]
        for key, value in result["data_schema"].schema.items():
            key = str(key)
            if hasattr(value, "schema") and hasattr(value, "options"):        # a section
                section_strings = step_strings["sections"][key]
                assert "name" in section_strings, key
                for inner in value.schema.schema:
                    assert str(inner) in section_strings["data"], (key, inner)
            else:
                assert key in step_strings["data"], (result["step_id"], key)

    # Settings: every setting is labelled and described in its section
    flow_id = await open_options(hass, entry)
    result = await menu(hass, flow_id, "settings")
    settings_strings = strings["options"]["step"]["settings"]
    check_form(settings_strings, result)
    in_form = set()
    for section_key, section_strings in settings_strings["sections"].items():
        for key in section_strings["data"]:
            assert key in section_strings.get("data_description", {}), key
            in_form.add(key)
    for spec in SETTINGS:
        assert spec.key in in_form, spec.key
        if spec.kind == "select":
            assert set(spec.options) <= set(strings["selector"][spec.key]["options"]), spec.key
    hass.config_entries.options.async_abort(flow_id)

    # Configure menu and import
    assert set(strings["options"]["step"]["init"]["menu_options"]) == {"settings", "import_yaml"}
    config_file("hm_translation_check.yaml", LEGACY_YAML)
    flow_id = await open_options(hass, entry)
    result = await menu(hass, flow_id, "import_yaml")
    check_form(strings["options"]["step"]["import_yaml"], result)
    result = await form(hass, flow_id, {"path": "hm_translation_check.yaml"})
    check_form(strings["options"]["step"]["import_yaml_confirm"], result)
    hass.config_entries.options.async_abort(flow_id)
    for key in ("file_not_found", "invalid_yaml", "path_outside_config", "no_zones"):
        assert key in strings["options"]["error"]

    # Zone and room forms, add and reconfigure
    sub_strings = strings["config_subentries"]
    zone = subentries(entry, "zone")["downstairs"]
    room = subentries(entry, "room")["lounge"]
    for kind, subentry_id in (("zone", None), ("zone", zone.subentry_id), ("room", None), ("room", room.subentry_id)):
        result = await start_subentry(hass, entry, kind, subentry_id)
        check_form(sub_strings[kind]["step"][result["step_id"]], result)
        hass.config_entries.subentries.async_abort(result["flow_id"])
    for key in ("name_required", "name_in_use", "invalid_time", "overlap"):
        assert key in sub_strings["zone"]["error"]
    for key in ("name_required", "zone_missing", "room_empty", "trv_in_use"):
        assert key in sub_strings["room"]["error"]
    assert "no_zones" in sub_strings["room"]["abort"]
    for kind in ("zone", "room"):
        assert "reconfigure_successful" in sub_strings[kind]["abort"]
        assert "entry_type" in sub_strings[kind]

    assert "name" in strings["config"]["step"]["user"]["data"]
    for key in ("zone_heating_demand_mode", "fallback_mode", "heating_demand_mode"):
        assert key in strings["selector"]
    assert set(strings["selector"]["schedule_period"]["fields"]) == {"start", "end", "temperature"}
    assert set(strings["selector"]["last_seen_sensor"]["fields"]) == {"temperature", "last_seen"}
    assert "yaml_imported" in strings["issues"]
